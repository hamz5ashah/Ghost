; Ghost can still be running in the tray when an update is installed, together
; with its helpers (phantom.exe, llama-server.exe, uv.exe). Windows refuses to
; overwrite a file in use ("Error opening file for writing"), so stop every
; process started from this install folder first. Ghost itself goes first and
; the check repeats for up to ~10 s: otherwise Ghost can restart a helper it
; just lost. Processes elsewhere (e.g. your own Ollama) are left alone.
;
; The installer is 32-bit, so this PowerShell is too, and its Get-Process
; cannot read the paths of 64-bit programs; Win32_Process (CIM) reports
; ExecutablePath for every process, so that is used instead.
!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$d = '$INSTDIR'.TrimEnd('\') + '\'; $$main = $$d + '${MAINBINARYNAME}.exe'; for ($$i = 0; $$i -lt 20; $$i++) { $$p = @(Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$d, [StringComparison]::OrdinalIgnoreCase) }); if ($$p.Count -eq 0) { break }; $$p | Sort-Object { if ($$_.ExecutablePath -ieq $$main) { 0 } else { 1 } } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 500 }"`
  Pop $0
!macroend
