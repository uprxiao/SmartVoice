; Only the stable application-owned login item is removed; user data is preserved.
!macro customUnInstall
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "SmartVoiceInput"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "SmartVoiceInput"
!macroend
