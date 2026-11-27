using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

namespace AutoGradeLauncher
{
    static class Program
    {
        private const int Port = 8888;
        private static readonly string HealthUrl = "http://127.0.0.1:" + Port + "/api/health";
        private static readonly string AppUrl = "http://127.0.0.1:" + Port + "/";

        [STAThread]
        static void Main()
        {
            Application.EnableVisualStyles();
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            string serveScript = Path.Combine(root, "tools", "工作台", "serve.py");

            if (!File.Exists(serveScript))
            {
                MessageBox.Show(
                    "未找到工作台程序：\n" + serveScript + "\n\n请确保本启动器放在「自动改卷」项目根目录下。",
                    "自动改卷工作台",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return;
            }

            if (IsServerHealthy())
            {
                OpenAppWindow(AppUrl);
                return;
            }

            string python = FindPython(root);
            if (string.IsNullOrEmpty(python))
            {
                string prompt = "请帮我在这台 Windows 电脑上安装 Python 3.11~3.13（勾选 Add to PATH），然后进入项目目录 `" + root + "` 运行 `python tools/工作台.py check` 检查自动改卷环境。";
                try { Clipboard.SetText(prompt); } catch { }
                MessageBox.Show(
                    "未在本机检测到 Python 解释器。\n\n已将给 AI 助手（如 opencode）的诊断提示词复制到剪贴板，打开 AI 助手直接 Ctrl+V 粘贴发送即可获得一键安装指导。",
                    "自动改卷工作台 - 缺少 Python",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning
                );
                return;
            }

            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = python,
                    Arguments = "\"" + serveScript + "\" --port " + Port,
                    WorkingDirectory = root,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                };
                Process.Start(psi);
            }
            catch (Exception ex)
            {
                MessageBox.Show("启动本地工作台失败：\n" + ex.Message, "自动改卷工作台", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            // 等待本地服务就绪（最多 10 秒）
            bool ready = false;
            for (int i = 0; i < 40; i++)
            {
                if (IsServerHealthy()) { ready = true; break; }
                Thread.Sleep(250);
            }

            if (!ready)
            {
                MessageBox.Show(
                    "工作台服务启动超时（端口 " + Port + " 未响应）。\n可在项目目录运行 `python tools\\工作台.py serve --open` 查看详细日志。",
                    "自动改卷工作台",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning
                );
                return;
            }

            OpenAppWindow(AppUrl);
        }

        private static bool IsServerHealthy()
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(HealthUrl);
                req.Timeout = 600;
                req.ReadWriteTimeout = 600;
                using (var resp = (HttpWebResponse)req.GetResponse())
                {
                    return resp.StatusCode == HttpStatusCode.OK;
                }
            }
            catch
            {
                return false;
            }
        }

        private static string FindPython(string root)
        {
            // 0. 优先使用项目内独立虚拟环境或嵌入式 Python（避免系统全局目录权限冲突）
            string[] localVenvs = {
                Path.Combine(root, ".venv", "Scripts", "python.exe"),
                Path.Combine(root, "venv", "Scripts", "python.exe"),
                Path.Combine(root, "python_embed", "python.exe")
            };
            foreach (string vp in localVenvs)
            {
                if (File.Exists(vp)) return vp;
            }

            // 1. PATH 查找
            string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string dir in pathEnv.Split(';'))
            {
                if (string.IsNullOrWhiteSpace(dir)) continue;
                try
                {
                    string candidate = Path.Combine(dir.Trim(), "python.exe");
                    if (File.Exists(candidate) && candidate.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) < 0)
                        return candidate;
                }
                catch { }
            }

            // 2. 常见路径兜底（含 Miniconda / Anaconda / py.exe）
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            string[] roots = {
                @"D:\Dev\Python",
                Path.Combine(local, "Programs", "Python"),
                Path.Combine(profile, "miniconda3"),
                Path.Combine(profile, "anaconda3"),
                Path.Combine(local, "miniconda3"),
                Path.Combine(local, "anaconda3"),
                @"C:\ProgramData\miniconda3",
                @"C:\ProgramData\anaconda3",
                @"C:\Python313",
                @"C:\Python312",
                @"C:\Python311"
            };
            foreach (string r in roots)
            {
                if (!Directory.Exists(r)) continue;
                string direct = Path.Combine(r, "python.exe");
                if (File.Exists(direct)) return direct;
                try
                {
                    foreach (string sub in Directory.GetDirectories(r))
                    {
                        string p = Path.Combine(sub, "python.exe");
                        if (File.Exists(p)) return p;
                    }
                }
                catch { }
            }

            // 3. Windows Python Launcher (py.exe)
            string pyLauncher = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "py.exe");
            if (File.Exists(pyLauncher)) return pyLauncher;
            return null;
        }

        private static void OpenAppWindow(string url)
        {
            string[] browsers = {
                @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                @"C:\Program Files\Google\Chrome\Application\chrome.exe"
            };
            foreach (string exe in browsers)
            {
                if (File.Exists(exe))
                {
                    try
                    {
                        Process.Start(new ProcessStartInfo { FileName = exe, Arguments = "--app=" + url, UseShellExecute = false });
                        return;
                    }
                    catch { }
                }
            }
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
        }
    }
}
