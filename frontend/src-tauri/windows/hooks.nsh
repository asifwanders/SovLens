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
