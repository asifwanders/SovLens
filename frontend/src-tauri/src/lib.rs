#[allow(unused_imports)]
use tauri_plugin_updater::UpdaterExt;
#[allow(unused_imports)]
use tauri::Manager;
#[allow(unused_imports)]
use tauri::RunEvent;
#[allow(unused_imports)]
use std::sync::{Arc, Mutex};

/// Shared handle to the spawned PyInstaller backend sidecar so we can kill it
/// on app exit. Without this the sidecar lives on after cmd+Q (mac) / window
/// close (win), holds port 14793, and the next launch hangs forever waiting
/// for the port to free.
///
/// We moved from `app.shell().sidecar(...)` (Tauri's externalBin) to a
/// manual `std::process::Command` invocation because PyInstaller is now in
/// onedir mode — the executable cannot be shipped as a lone externalBin
/// without breaking its sibling `_internal/` resolution at runtime. Onefile
/// mode caused NSIS mmap failures on Win CI (file too large).
#[derive(Default)]
struct SidecarState(Arc<Mutex<Option<std::process::Child>>>);

#[derive(serde::Serialize)]
pub struct UpdateInfo {
    pub version: String,
    pub body: Option<String>,
}

#[tauri::command]
async fn check_for_updates(app: tauri::AppHandle) -> Result<Option<UpdateInfo>, String> {
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(update)) => Ok(Some(UpdateInfo {
            version: update.version.clone(),
            body: update.body.clone(),
        })),
        Ok(None) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

fn install_panic_logger() {
    let log_dir = match dirs::data_local_dir().or_else(dirs::home_dir) {
        Some(p) => p.join("SovLens").join("logs"),
        None => return,
    };
    let _ = std::fs::create_dir_all(&log_dir);

    let log_path = log_dir.join("panic.log");
    std::panic::set_hook(Box::new(move |info| {
        let ts = chrono::Utc::now().to_rfc3339();
        let payload = format!("[{}] PANIC: {}\n{}\n\n", ts, info, std::backtrace::Backtrace::force_capture());
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
            use std::io::Write;
            let _ = f.write_all(payload.as_bytes());
        }
        eprintln!("{}", payload);
    }));
}

#[tauri::command]
fn open_logs_folder() -> Result<(), String> {
    let dir = dirs::data_local_dir().or_else(dirs::home_dir)
        .map(|p| p.join("SovLens").join("logs"))
        .ok_or("could not resolve logs dir")?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    std::process::Command::new("open").arg(&dir).spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "windows")]
    std::process::Command::new("explorer.exe").arg(&dir).spawn().map_err(|e| e.to_string())?;
    #[cfg(target_os = "linux")]
    std::process::Command::new("xdg-open").arg(&dir).spawn().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn read_recent_logs(max_bytes: usize) -> Result<String, String> {
    let dir = dirs::data_local_dir().or_else(dirs::home_dir)
        .map(|p| p.join("SovLens").join("logs"))
        .ok_or("no logs dir")?;
    let mut out = String::new();
    for name in ["panic.log", "backend.log"] {
        let path = dir.join(name);
        if !path.exists() { continue; }
        let bytes = std::fs::read(&path).map_err(|e| e.to_string())?;
        let start = bytes.len().saturating_sub(max_bytes);
        out.push_str(&format!("===== {} =====\n", name));
        out.push_str(&String::from_utf8_lossy(&bytes[start..]));
        out.push_str("\n\n");
    }
    Ok(out)
}

use std::path::Path;

#[tauri::command]
fn copy_media_to_clipboard(path: String) -> Result<(), String> {
    let p = Path::new(&path);
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    // HEIC/HEIF require libheif system dep that the `image` crate's default features
    // don't include. Fall through to file-reference copy for those.
    let is_image = matches!(ext.as_str(),
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "bmp");

    if is_image {
        copy_image_bitmap(&path)
    } else {
        copy_file_reference(&path)
    }
}

fn copy_image_bitmap(path: &str) -> Result<(), String> {
    let img = image::open(path).map_err(|e| format!("decode failed: {}", e))?;
    let rgba = img.to_rgba8();
    let (w, h) = rgba.dimensions();
    let mut cb = arboard::Clipboard::new().map_err(|e| e.to_string())?;
    cb.set_image(arboard::ImageData {
        width: w as usize,
        height: h as usize,
        bytes: std::borrow::Cow::Owned(rgba.into_raw()),
    }).map_err(|e| e.to_string())
}

#[cfg(target_os = "macos")]
fn copy_file_reference(path: &str) -> Result<(), String> {
    use std::process::Command;
    let abs = std::fs::canonicalize(path).map_err(|e| e.to_string())?;
    let abs_str = abs.to_string_lossy();
    // Escape backslash FIRST (else later quote-escaping doubles the backslashes),
    // then escape double-quote. Backslashes in macOS paths are rare but legal.
    let escaped = abs_str.replace('\\', "\\\\").replace('"', "\\\"");
    let script = format!("set the clipboard to (POSIX file \"{}\")", escaped);
    Command::new("osascript").args(["-e", &script])
        .status().map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(target_os = "windows")]
fn copy_file_reference(path: &str) -> Result<(), String> {
    use clipboard_win::{formats::FileList, Clipboard, Setter};
    let files = [path.to_string()];
    let _clip = Clipboard::new_attempts(10).map_err(|e| e.to_string())?;
    FileList.write_clipboard(&files[..]).map_err(|e| e.to_string())
}

#[cfg(target_os = "linux")]
fn copy_file_reference(path: &str) -> Result<(), String> {
    use std::process::{Command, Stdio};
    use std::io::Write;
    let mut child = Command::new("xclip")
        .args(["-selection", "clipboard", "-t", "text/uri-list"])
        .stdin(Stdio::piped()).spawn().map_err(|e| e.to_string())?;
    if let Some(stdin) = child.stdin.as_mut() {
        write!(stdin, "file://{}", path).map_err(|e| e.to_string())?;
    }
    child.wait().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn reveal_in_explorer(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer.exe")
            .arg(format!("/select,{}", path))
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .args(["-R", &path])
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "linux")]
    {
        let parent = std::path::Path::new(&path)
            .parent()
            .ok_or("no parent")?;
        std::process::Command::new("xdg-open")
            .arg(parent)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Set the macOS Dock icon at runtime from the bundled icon PNG.
/// Needed in dev mode where the raw exe has no .app bundle Info.plist.
#[cfg(target_os = "macos")]
fn set_macos_dock_icon() {
    use objc2::msg_send;
    use objc2::runtime::AnyObject;

    // Embed icon at compile time so prod builds also benefit.
    const ICON_BYTES: &[u8] = include_bytes!("../icons/icon.png");

    unsafe {
        let ns_data_cls = objc2::runtime::AnyClass::get("NSData").expect("NSData class");
        let ns_image_cls = objc2::runtime::AnyClass::get("NSImage").expect("NSImage class");
        let ns_app_cls = objc2::runtime::AnyClass::get("NSApplication").expect("NSApplication class");

        let data: *mut AnyObject = msg_send![
            ns_data_cls,
            dataWithBytes: ICON_BYTES.as_ptr() as *const std::ffi::c_void,
            length: ICON_BYTES.len()
        ];
        if data.is_null() { return; }

        let image_alloc: *mut AnyObject = msg_send![ns_image_cls, alloc];
        let image: *mut AnyObject = msg_send![image_alloc, initWithData: data];
        if image.is_null() { return; }

        let app: *mut AnyObject = msg_send![ns_app_cls, sharedApplication];
        let _: () = msg_send![app, setApplicationIconImage: image];
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    install_panic_logger();
    let sidecar_state = SidecarState::default();
    #[allow(unused_variables)]
    let sidecar_slot_for_exit = sidecar_state.0.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_os::init())
        .manage(sidecar_state)
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // macOS: raw dev binary has no Info.plist, so the Dock falls back to the
            // generic Tauri icon. Set it at runtime from the bundled icon PNG.
            #[cfg(target_os = "macos")]
            set_macos_dock_icon();

            // Spawn the Python backend sidecar only in release builds.
            // In dev mode, run `python main.py` separately as before.
            //
            // Onedir layout — the PyInstaller bootloader EXE lives at
            //   <resource_dir>/sovlens-backend/sovlens-backend(.exe)
            // alongside `_internal/` (which the bootloader looks for as a
            // sibling). Tauri's externalBin would split exe and _internal
            // across Contents/MacOS/ + Contents/Resources/ on mac, which
            // breaks the loader. Manual std::process::Command lets us
            // launch with the working directory and arguments we control.
            #[cfg(not(debug_assertions))]
            {
                use std::io::Write;

                // Logs dir (same as panic_logger uses)
                let log_dir = dirs::data_local_dir()
                    .or_else(dirs::home_dir)
                    .map(|p| p.join("SovLens").join("logs"))
                    .expect("logs dir");
                let _ = std::fs::create_dir_all(&log_dir);
                let backend_log = log_dir.join("backend.log");

                // Always write a startup stamp so users have evidence the
                // shell got this far even if the sidecar itself never starts.
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true).append(true).open(&backend_log)
                {
                    let _ = writeln!(
                        f,
                        "[{}] BOOT tauri shell starting sidecar (onedir)",
                        chrono::Utc::now().to_rfc3339()
                    );
                }

                let exe_name = if cfg!(target_os = "windows") {
                    "sovlens-backend.exe"
                } else {
                    "sovlens-backend"
                };

                let resolved = app.path()
                    .resolve(format!("sovlens-backend/{}", exe_name),
                             tauri::path::BaseDirectory::Resource);

                match resolved {
                    Ok(exe_path) => {
                        // Ensure the bootloader is executable on Unix.
                        // Tauri's bundle.resources doesn't always preserve
                        // the +x bit; setting it idempotently is cheap and
                        // makes the spawn robust across mac/linux packagers.
                        #[cfg(unix)]
                        {
                            use std::os::unix::fs::PermissionsExt;
                            if let Ok(meta) = std::fs::metadata(&exe_path) {
                                let mut perms = meta.permissions();
                                if perms.mode() & 0o111 == 0 {
                                    perms.set_mode(perms.mode() | 0o755);
                                    let _ = std::fs::set_permissions(&exe_path, perms);
                                }
                            }
                        }

                        let mut cmd = std::process::Command::new(&exe_path);
                        cmd.args(["--port", "14793"])
                            .stdout(std::process::Stdio::piped())
                            .stderr(std::process::Stdio::piped());

                        // Hide the console window on Windows. Without
                        // CREATE_NO_WINDOW (0x08000000) a conhost flashes
                        // up every launch because the bootloader is a
                        // console subsystem binary.
                        #[cfg(target_os = "windows")]
                        {
                            use std::os::windows::process::CommandExt;
                            cmd.creation_flags(0x08000000);
                        }

                        match cmd.spawn() {
                            Ok(mut child) => {
                                if let Ok(mut f) = std::fs::OpenOptions::new()
                                    .create(true).append(true).open(&backend_log)
                                {
                                    let _ = writeln!(
                                        f,
                                        "[{}] BOOT sidecar spawned pid={} exe={}",
                                        chrono::Utc::now().to_rfc3339(),
                                        child.id(),
                                        exe_path.display()
                                    );
                                }

                                // Drain stdout/stderr on background threads
                                // so the pipe buffer never fills and stalls
                                // the backend at the first ~64 KB of logs.
                                let stdout = child.stdout.take();
                                let stderr = child.stderr.take();
                                let log_out = backend_log.clone();
                                let log_err = backend_log.clone();
                                if let Some(out) = stdout {
                                    std::thread::spawn(move || {
                                        use std::io::{BufRead, BufReader};
                                        let reader = BufReader::new(out);
                                        for line in reader.lines().map_while(Result::ok) {
                                            if let Ok(mut f) = std::fs::OpenOptions::new()
                                                .create(true).append(true).open(&log_out)
                                            {
                                                let _ = writeln!(
                                                    f, "[{}] OUT {}",
                                                    chrono::Utc::now().to_rfc3339(), line
                                                );
                                            }
                                        }
                                    });
                                }
                                if let Some(err) = stderr {
                                    std::thread::spawn(move || {
                                        use std::io::{BufRead, BufReader};
                                        let reader = BufReader::new(err);
                                        for line in reader.lines().map_while(Result::ok) {
                                            if let Ok(mut f) = std::fs::OpenOptions::new()
                                                .create(true).append(true).open(&log_err)
                                            {
                                                let _ = writeln!(
                                                    f, "[{}] ERR {}",
                                                    chrono::Utc::now().to_rfc3339(), line
                                                );
                                            }
                                        }
                                    });
                                }

                                // Park the Child so RunEvent::Exit can kill
                                // it. Dropping here would leak port 14793.
                                let sidecar_slot = app.state::<SidecarState>().0.clone();
                                if let Ok(mut slot) = sidecar_slot.lock() {
                                    *slot = Some(child);
                                }
                            }
                            Err(e) => {
                                if let Ok(mut f) = std::fs::OpenOptions::new()
                                    .create(true).append(true).open(&backend_log)
                                {
                                    let _ = writeln!(
                                        f,
                                        "[{}] ERR sidecar spawn failed: {} (exe={})",
                                        chrono::Utc::now().to_rfc3339(),
                                        e,
                                        exe_path.display()
                                    );
                                }
                            }
                        }
                    }
                    Err(e) => {
                        if let Ok(mut f) = std::fs::OpenOptions::new()
                            .create(true).append(true).open(&backend_log)
                        {
                            let _ = writeln!(
                                f,
                                "[{}] ERR resource path resolve failed: {}",
                                chrono::Utc::now().to_rfc3339(),
                                e
                            );
                        }
                    }
                }
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![reveal_in_explorer, copy_media_to_clipboard, open_logs_folder, read_recent_logs, check_for_updates])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app_handle, event| {
            // Kill the sidecar on app exit so port 14793 is freed for the next
            // launch. Without this, cmd+Q on macOS leaves the PyInstaller
            // child running and the next launch hangs.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                if let Ok(mut slot) = sidecar_slot_for_exit.lock() {
                    if let Some(mut child) = slot.take() {
                        let _ = child.kill();
                        // Reap so we don't leave a zombie on Unix.
                        let _ = child.wait();
                    }
                }
            }
        });
}
