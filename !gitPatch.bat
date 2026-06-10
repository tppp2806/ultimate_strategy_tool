@echo off
setlocal EnableExtensions DisableDelayedExpansion

REM ============================================================
REM 通用 Git Patch 拖拽应用脚本
REM 用法：
REM   1. 把本 bat 放到 Git 仓库根目录，也就是 .git 同级目录
REM   2. 把 .patch / .diff 文件拖到本 bat 上
REM   3. 脚本会先 git apply --check，再真正应用
REM
REM 支持：
REM   - 普通 git diff / .patch / .diff：使用 git apply
REM   - git format-patch 生成的邮件补丁：自动尝试 git am --3way
REM   - 多个 patch 文件一次拖入
REM ============================================================

title Git Patch 拖拽应用器

pushd "%~dp0" >nul

where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 git 命令。请先安装 Git for Windows，并确保 git 在 PATH 中。
    echo.
    pause
    exit /b 1
)

for /f "delims=" %%R in ('git rev-parse --show-toplevel 2^>nul') do set "REPO_ROOT=%%R"
if not defined REPO_ROOT (
    echo [错误] 当前 bat 所在位置不是 Git 仓库内。
    echo 请把本 bat 放到 .git 同级目录，也就是仓库根目录。
    echo 当前目录：%CD%
    echo.
    pause
    exit /b 1
)

cd /d "%REPO_ROOT%"
echo [仓库] %REPO_ROOT%
echo.

if "%~1"=="" (
    echo [提示] 请把 .patch 或 .diff 文件拖到这个 bat 上运行。
    echo.
    set /p "PATCH_INPUT=也可以手动输入 patch 文件完整路径："
    if "%PATCH_INPUT%"=="" (
        echo 未输入 patch 文件，已退出。
        echo.
        pause
        exit /b 1
    )
    call :APPLY_ONE "%PATCH_INPUT%"
    goto :END
)

REM 检查工作区是否已有改动
for /f "delims=" %%S in ('git status --porcelain') do (
    set "HAS_CHANGES=1"
    goto :STATUS_CHECK_DONE
)
:STATUS_CHECK_DONE

if defined HAS_CHANGES (
    echo [警告] 当前工作区已有未提交改动。
    echo 建议先在 GitHub Desktop 中确认当前改动，或者先提交/暂存，避免 patch 冲突时不好回滚。
    echo.
    choice /c YN /n /m "是否仍然继续应用 patch？[Y/N] "
    if errorlevel 2 (
        echo 已取消。
        echo.
        pause
        exit /b 1
    )
    echo.
)

set "ANY_FAILED="
for %%F in (%*) do (
    call :APPLY_ONE "%%~fF"
    if errorlevel 1 set "ANY_FAILED=1"
    echo.
)

goto :END


:APPLY_ONE
set "PATCH_FILE=%~1"
set "LOG_FILE=%TEMP%\git_patch_apply_%RANDOM%_%RANDOM%.log"

echo ============================================================
echo [处理] %PATCH_FILE%
echo ============================================================

if not exist "%PATCH_FILE%" (
    echo [错误] 文件不存在：%PATCH_FILE%
    exit /b 1
)

REM 判断是否是 git format-patch 邮件补丁
set "IS_MAIL_PATCH="
findstr /b /c:"From " "%PATCH_FILE%" >nul 2>nul
if not errorlevel 1 (
    findstr /b /c:"Subject:" "%PATCH_FILE%" >nul 2>nul
    if not errorlevel 1 set "IS_MAIL_PATCH=1"
)

if defined IS_MAIL_PATCH (
    echo [识别] 这是 git format-patch 邮件补丁，使用 git am --3way 应用。
    echo.
    git am --3way --keep-cr "%PATCH_FILE%"
    if errorlevel 1 (
        echo.
        echo [失败] git am 应用失败。
        echo 你可以用下面命令取消这次 am：
        echo   git am --abort
        echo.
        echo 然后在 GitHub Desktop 或编辑器中查看冲突。
        exit /b 1
    )
    echo [成功] 已应用邮件补丁。
    exit /b 0
)

echo [检查] git apply --check
git apply --check --whitespace=fix "%PATCH_FILE%" >"%LOG_FILE%" 2>&1
if not errorlevel 1 (
    echo [应用] git apply --whitespace=fix
    git apply --whitespace=fix "%PATCH_FILE%"
    if errorlevel 1 (
        echo [失败] 检查通过但应用失败，详细信息：
        type "%LOG_FILE%"
        del "%LOG_FILE%" >nul 2>nul
        exit /b 1
    )
    echo [成功] patch 已应用。
    del "%LOG_FILE%" >nul 2>nul
    exit /b 0
)

echo [提示] 直接应用检查失败，原因如下：
type "%LOG_FILE%"
echo.

choice /c YN /n /m "是否尝试 3-way 合并应用？[Y/N] "
if errorlevel 2 goto :TRY_REJECT

echo.
echo [检查] git apply --3way --check
git apply --3way --check --whitespace=fix "%PATCH_FILE%" >"%LOG_FILE%" 2>&1
if not errorlevel 1 (
    echo [应用] git apply --3way --whitespace=fix
    git apply --3way --whitespace=fix "%PATCH_FILE%"
    if errorlevel 1 (
        echo.
        echo [失败] 3-way 应用失败。可能已经留下冲突标记，请用 git status 查看。
        type "%LOG_FILE%"
        del "%LOG_FILE%" >nul 2>nul
        exit /b 1
    )
    echo [成功] patch 已通过 3-way 应用。
    del "%LOG_FILE%" >nul 2>nul
    exit /b 0
)

echo [提示] 3-way 检查也失败，原因如下：
type "%LOG_FILE%"
echo.

:TRY_REJECT
choice /c YN /n /m "是否尝试 --reject 部分应用？会生成 .rej 文件，需要手动处理。[Y/N] "
if errorlevel 2 (
    echo [取消] 未应用此 patch。
    del "%LOG_FILE%" >nul 2>nul
    exit /b 1
)

echo.
echo [应用] git apply --reject --whitespace=fix
git apply --reject --whitespace=fix "%PATCH_FILE%"
if errorlevel 1 (
    echo.
    echo [失败] --reject 也失败。未能自动应用。
    del "%LOG_FILE%" >nul 2>nul
    exit /b 1
)

echo.
echo [完成] 已尽量应用 patch。
echo 如果生成了 .rej 文件，说明有部分内容需要手动合并。
echo 可用下面命令查看：
echo   git status
echo.
del "%LOG_FILE%" >nul 2>nul
exit /b 0


:END
echo ============================================================
if defined ANY_FAILED (
    echo [结束] 有 patch 应用失败或被取消，请查看上面的错误信息。
) else (
    echo [结束] 所有 patch 已处理完成。
)
echo.
echo 当前 Git 状态：
git status --short
echo.
echo 你可以回到 GitHub Desktop 查看变更 diff。
echo ============================================================
popd >nul
pause
exit /b 0
