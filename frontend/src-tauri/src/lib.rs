#[allow(unused_imports)]
use tauri_plugin_shell::ShellExt;

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
    use clipboard_win::{formats, set_clipboard};
    set_clipboard(formats::FileList, &[path.to_string()])
        .map_err(|e| e.to_string())
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
    tauri::Builder::default()
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_os::init())
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
            #[cfg(not(debug_assertions))]
            {
                // Guard against the sidecar binary being absent or an empty
                // placeholder (common when PyInstaller step was skipped).
                let bin_path = app
                    .path()
                    .resolve("sovlens-backend", tauri::path::BaseDirectory::Resource);
                let should_spawn = match bin_path {
                    Ok(ref p) => std::fs::metadata(p)
                        .map(|m| m.len() > 4096)
                        .unwrap_or(false),
                    Err(_) => false,
                };
                if should_spawn {
                    let sidecar_command = app
                        .shell()
                        .sidecar("sovlens-backend")
                        .expect("failed to create sidecar")
                        .args(["--port", "14793"]);
                    let (_rx, _child) = sidecar_command.spawn().expect("failed to spawn sidecar");
                } else {
                    eprintln!(
                        "WARN: sovlens-backend sidecar binary missing or too small, skipping spawn"
                    );
                }
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![reveal_in_explorer, copy_media_to_clipboard])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
