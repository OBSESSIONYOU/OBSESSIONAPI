@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title OBSESSIONAPI 一键启动

set "URL=http://127.0.0.1:8000/"
set "DEPS=import fastapi, uvicorn, httpx, cryptography"

echo.
echo  ==========================================
echo    OBSESSIONAPI 一键启动
echo  ==========================================
echo.

if not exist "run.py" (
  echo  [错误] 未找到 run.py，请把本文件放在项目根目录。
  goto :fail
)

echo  [1/3] 准备 Python 运行环境...
set "PY="

rem ---------- 1a. 优先使用 python-env.txt 指定的解释器 ----------
set "CFGPY="
if exist "python-env.txt" (
  for /f "usebackq eol=; delims=" %%P in ("python-env.txt") do if not defined CFGPY set "CFGPY=%%P"
)
if not defined CFGPY goto :try_venv
if not exist "%CFGPY%" (
  echo  [警告] python-env.txt 里的解释器不存在，改为自动探测。
  goto :try_venv
)
echo         使用指定解释器: %CFGPY%
"%CFGPY%" -c "%DEPS%" >nul 2>nul
if not errorlevel 1 (
  set "PY=%CFGPY%"
  goto :py_ready
)
echo         该环境缺少依赖，正在安装到它里面（不会写入 base）...
"%CFGPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo  [错误] 依赖安装失败，请检查网络后重试。
  goto :fail
)
set "PY=%CFGPY%"
goto :py_ready

rem ---------- 1b. 自动探测 ----------
:try_venv
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%DEPS%" >nul 2>nul
  if not errorlevel 1 (
    set "PY=.venv\Scripts\python.exe"
    goto :py_ready
  )
)
py -3 -c "%DEPS%" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  goto :py_ready
)
python -c "%DEPS%" >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
  goto :py_ready
)

set "BASE="
py -3 -V >nul 2>nul
if not errorlevel 1 set "BASE=py -3"
if not defined BASE (
  python -V >nul 2>nul
  if not errorlevel 1 set "BASE=python"
)
if not defined BASE (
  echo  [错误] 未找到任何可用的 Python。
  echo         请安装 Python 3.10+，或在 python-env.txt 里写出解释器完整路径。
  goto :fail
)
echo         未找到现成可用环境，作为兜底在项目内建 .venv（首次较慢）...
%BASE% -m venv .venv
if errorlevel 1 (
  echo  [错误] 创建 .venv 失败。
  goto :fail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo  [错误] 依赖安装失败，请检查网络后重试。
  goto :fail
)
set "PY=.venv\Scripts\python.exe"

:py_ready
set "PYVER=?"
for /f "tokens=2" %%V in ('%PY% -V 2^>^&1') do set "PYVER=%%V"
echo         使用 Python !PYVER!，依赖完整

rem ---------- 2/3 端口占用 ----------
%PY% -c "import socket,sys;s=socket.socket();s.settimeout(0.5);sys.exit(0 if s.connect_ex(('127.0.0.1',8000))==0 else 1)" >nul 2>nul
if not errorlevel 1 (
  echo  [2/3] 8000 端口已有服务在监听，复用它。
) else (
  echo  [2/3] 8000 端口空闲，正在启动服务...
  start "OBSESSIONAPI 服务 - 关闭此窗口即停止" cmd /k "%PY% run.py"
)

rem ---------- 3/3 等就绪再开浏览器 ----------
echo  [3/3] 等待服务就绪（最多 60 秒）...
set "READY="
for /l %%I in (1,1,60) do (
  %PY% -c "import urllib.request,sys;urllib.request.urlopen('%URL%health',timeout=1);sys.exit(0)" >nul 2>nul
  if not errorlevel 1 (
    set "READY=1"
    goto :server_ready
  )
)

:server_ready
if not defined READY (
  echo  [错误] 60 秒内没有等到服务就绪。
  echo         请查看标题为 "OBSESSIONAPI 服务" 的窗口里的报错信息。
  goto :fail
)

start "" "%URL%"
echo         服务已就绪，已在浏览器打开控制台。
echo.
echo  控制台地址: %URL%
echo  未登录时会直接显示登录界面；首次使用会先显示创建管理员账户。
echo  日志在另一个标题为 "OBSESSIONAPI 服务" 的窗口。
echo  停止服务: 关闭那个窗口即可。
echo.
timeout /t 8 /nobreak >nul
goto :done

:fail
echo.
echo  启动未完成。按任意键关闭本窗口。
pause >nul
endlocal
exit /b 1

:done
endlocal
exit /b 0
