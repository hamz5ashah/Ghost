; Ghost can still be running in the tray when an update is installed, together
; with its helpers (ollama.exe, uv.exe). Windows refuses to overwrite a file in
; use ("Error opening file for writing"), so stop every process started from
; this install folder first. Processes elsewhere (e.g. your own Ollama) are left alone.
!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$dir = '$INSTDIR'.TrimEnd('\') + '\'; Get-Process | Where-Object { $$_.Path -and $$_.Path.StartsWith($$dir, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue"`
  Pop $0
  Sleep 1500
!macroend
