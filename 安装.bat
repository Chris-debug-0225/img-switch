@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在为“IMG Switch”创建快捷方式...
powershell -NoProfile -Command ^
 "$ws = New-Object -ComObject WScript.Shell;" ^
 "$py = 'pythonw.exe';" ^
 "$desktop = [Environment]::GetFolderPath('Desktop');" ^
 "$programs = [Environment]::GetFolderPath('Programs');" ^
 "$args = '\"%~dp0app.py\" gui';" ^
 "$wd = '%~dp0';" ^
 "Remove-Item -ErrorAction SilentlyContinue ($desktop + '\中转站生图.lnk'), ($programs + '\中转站生图.lnk');" ^
 "$s1 = $ws.CreateShortcut($desktop + '\IMG Switch.lnk'); $s1.TargetPath = $py; $s1.Arguments = $args; $s1.WorkingDirectory = $wd; $s1.Save();" ^
 "$s2 = $ws.CreateShortcut($programs + '\IMG Switch.lnk'); $s2.TargetPath = $py; $s2.Arguments = $args; $s2.WorkingDirectory = $wd; $s2.Save();"
if errorlevel 1 (
  echo 快捷方式创建失败，请检查 PowerShell 是否可用。
) else (
  echo 完成！桌面和开始菜单已创建“IMG Switch”（旧名“中转站生图”快捷方式已清理）。
)
echo 首次使用：双击“IMG Switch”→「总开关」页接入要用的 Agent →「中转源」页添加中转源。
pause
