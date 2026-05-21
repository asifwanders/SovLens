; SovLens NSIS installer hooks.
;
; Tauri's default NSIS template kills the main app exe (SovLens.exe) before
; extracting files, but the sidecar (sovlens-backend.exe) is a separate
; process that keeps a write lock on its own .exe. Without these hooks,
; upgrades fail with "Error opening file for writing: ...sovlens-backend.exe".
;
; nsExec::Exec (not ExecWait) so a missing process / non-zero exit doesn't
; abort the installer. taskkill returns 128 when no match — swallowed.

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Stopping SovLens backend sidecar..."
  nsExec::Exec 'taskkill /F /IM sovlens-backend.exe /T'
  Sleep 500
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Stopping SovLens backend sidecar..."
  nsExec::Exec 'taskkill /F /IM sovlens-backend.exe /T'
  Sleep 500
!macroend

; After uninstall, optionally wipe the app data dir (LanceDB index, logs,
; transcoded HLS cache, YOLO crops, folders.json, progress.json). Model
; caches under %USERPROFILE% (HuggingFace, Whisper, EasyOCR) are deliberately
; left alone because other AI tools on the machine may share them.
!macro NSIS_HOOK_POSTUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Also delete SovLens data (search index, logs, cache)?$\r$\n$\r$\nYou'll lose your search index and will need to re-scan your folders if you reinstall." \
    /SD IDNO IDNO sovlens_keep_data
    DetailPrint "Removing SovLens app data..."
    RMDir /r "$LOCALAPPDATA\SovLens"
  sovlens_keep_data:
!macroend
