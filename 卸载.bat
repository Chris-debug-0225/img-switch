@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在恢复本机原状（关闭 IMG Switch，配置保留）...
python "%~dp0app.py" off
echo 正在删除快捷方式...
powershell -NoProfile -Command ^
 "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Desktop') + '\IMG Switch.lnk'), ([Environment]::GetFolderPath('Programs') + '\IMG Switch.lnk'), ([Environment]::GetFolderPath('Desktop') + '\中转站生图.lnk'), ([Environment]::GetFolderPath('Programs') + '\中转站生图.lnk');"
echo 卸载完成（配置仍保留在 %%APPDATA%%\failover-imagegen-tool，删除该目录即可彻底清除）。
pause
