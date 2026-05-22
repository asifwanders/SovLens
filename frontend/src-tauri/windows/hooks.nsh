; SovLens NSIS installer hooks.
;
; Tauri's default NSIS template kills the main app exe (SovLens.exe) before
; extracting files, but the sidecar (sovlens-backend.exe) is a separate
; process that keeps a write lock on its own .exe. Without these hooks,
; upgrades fail with "Error opening file for writing: ...sovlens-backend.exe".
;
; nsExec::Exec (not ExecWait) so a missing process / non-zero exit doesn't
; abort the installer. taskkill returns 128 when no match — swallowed.

; Poll-wait helper: re-check tasklist up to 10× × 500ms after taskkill so
; the previous install's sidecar fully releases its .exe write lock before
; NSIS tries to overwrite it. Plain Sleep 500 leaves a race window where
; the process is "Terminating" but still holds the file.
!macro SOVLENS_KILL_SIDECAR_AND_WAIT
  DetailPrint "Stopping SovLens backend sidecar..."
  nsExec::Exec 'taskkill /F /IM sovlens-backend.exe /T'
  StrCpy $0 0
  ${Do}
    Sleep 500
    ; findstr exits 0 when sidecar still present, 1 when gone. Avoids
    ; LogicLib's missing string-contains operator (NSIS has no native
    ; one without the StrFunc.nsh plugin).
    nsExec::Exec 'cmd /c tasklist /FI "IMAGENAME eq sovlens-backend.exe" /NH | findstr /I "sovlens-backend.exe" >nul'
    Pop $1 ; exit code
    ${If} $1 != 0
      ${Break}
    ${EndIf}
    IntOp $0 $0 + 1
    ${If} $0 >= 10
      DetailPrint "Sidecar still running after 5s — proceeding anyway"
      ${Break}
    ${EndIf}
  ${Loop}
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro SOVLENS_KILL_SIDECAR_AND_WAIT
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro SOVLENS_KILL_SIDECAR_AND_WAIT
!macroend

; After uninstall: prompt twice.
;   1) Delete the SovLens app data dir (LanceDB index, logs, transcoded HLS
;      cache, YOLO crops, folders.json, progress.json).
;   2) Optionally also rm the shared HuggingFace / Whisper / EasyOCR caches
;      so a reinstall behaves as a true fresh install. Default = No because
;      those caches may belong to other AI tools on the machine.
!macro NSIS_HOOK_POSTUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Also delete SovLens data (search index, logs, cache)?$\r$\n$\r$\nYou'll lose your search index and will need to re-scan your folders if you reinstall." \
    /SD IDNO IDNO sovlens_keep_data
    DetailPrint "Removing SovLens app data..."
    RMDir /r "$LOCALAPPDATA\SovLens"

    ; Second prompt: AI model caches. These can be 1+ GB and are shared
    ; with other AI tools, so this is an explicit opt-in.
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "Also delete downloaded AI model files (~1 GB)?$\r$\n$\r$\nThese caches live under your user profile and may be shared with other AI tools (HuggingFace, Whisper, EasyOCR). Only delete them if you want a fully fresh start." \
      /SD IDNO IDNO sovlens_keep_caches
      DetailPrint "Removing AI model caches..."
      ; HuggingFace cache (CLIP + YOLO + Whisper via faster-whisper).
      RMDir /r "$PROFILE\.cache\huggingface"
      ; openai-whisper standalone cache (legacy — still wipe if present).
      RMDir /r "$PROFILE\.cache\whisper"
      ; EasyOCR models.
      RMDir /r "$PROFILE\.EasyOCR"
    sovlens_keep_caches:
  sovlens_keep_data:
!macroend
