@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在构建未签名 Windows 测试安装包...
echo 需要 Python 3.12、PyInstaller 6.16.0 和 PATH 中的 Inno Setup 6 ISCC.exe。
python packaging\windows\build_windows.py --version 0.1.0 --output dist\windows
if errorlevel 1 (
  echo 构建失败，请检查上面的错误信息。
) else (
  echo 构建完成。安装包尚未签名，请先执行安装与隐私检查。
)
pause
