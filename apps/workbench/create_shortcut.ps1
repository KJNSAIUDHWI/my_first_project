# -*- coding: utf-8 -*-
# 编译原生轻量启动器「自动改卷工作台.exe」并在桌面创建快捷方式
$ErrorActionPreference = "Stop"

$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent (Split-Path -Parent $ToolDir)
$CsFile  = Join-Path $ToolDir "launcher.cs"
$ExeFile = Join-Path $RootDir "自动改卷工作台.exe"

$csc = Get-ChildItem "C:\Windows\Microsoft.NET\Framework64\v4*\csc.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $csc) {
    $csc = Get-ChildItem "C:\Windows\Microsoft.NET\Framework\v4*\csc.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $csc) {
    throw "未找到 Windows 内置 csc.exe (.NET 4.x)"
}

Write-Host "正在编译启动器：$ExeFile"
& $csc /nologo /target:winexe /optimize+ /utf8output /r:System.Windows.Forms.dll /out:$ExeFile $CsFile
if ($LASTEXITCODE -ne 0) { throw "编译 launcher.cs 失败" }

$Desktop = [Environment]::GetFolderPath("Desktop")
$LnkPath = Join-Path $Desktop "自动改卷工作台.lnk"
$Wsh = New-Object -ComObject WScript.Shell
$Sc = $Wsh.CreateShortcut($LnkPath)
$Sc.TargetPath = $ExeFile
$Sc.WorkingDirectory = $RootDir
$Sc.Description = "启动自动改卷工作台（环境自检、数据入库、多人协作总览）"
$Sc.Save()

Write-Host "已完成："
Write-Host "  启动器：$ExeFile"
Write-Host "  桌面快捷方式：$LnkPath"
