using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// A real top-level Windows window receives session-end messages even when the
// services have no console. Closing the browser does not own the service lifetime.
internal static class Native
{
    internal const int QueryEndSession = 0x0011, EndSession = 0x0016;
    [DllImport("kernel32.dll")] internal static extern bool SetProcessShutdownParameters(uint level, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern bool ShutdownBlockReasonCreate(IntPtr window, string reason);
    [DllImport("user32.dll")] internal static extern bool ShutdownBlockReasonDestroy(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern uint RegisterWindowMessage(string name);
    [DllImport("user32.dll")] internal static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
}

internal interface ILauncherRuntime : IDisposable
{
    void Start();
    void RequestStop();
    void Stop();
    bool IsReady { get; }
    bool HasExited { get; }
    void OpenSite();
    void OpenLogs();
}

internal sealed class LauncherRuntime : ILauncherRuntime
{
    private readonly string root, stopFile, logFile;
    private readonly object logLock = new object();
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private Process supervisor;
    private bool started;
    private string instance;

    internal LauncherRuntime(string project)
    {
        root = Path.GetFullPath(project);
        Directory.CreateDirectory(Path.Combine(root, ".run"));
        Directory.CreateDirectory(Path.Combine(root, "logs"));
        stopFile = Path.Combine(root, ".run", "launcher." + Guid.NewGuid().ToString("N") + ".stop");
        logFile = Path.Combine(root, "logs", "launcher.log");
    }

    internal static string Quote(string value)
    {
        // CommandLineToArgvW/CRT quoting; never interpret paths as shell code.
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
            result.Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    private void Log(string text)
    {
        if (String.IsNullOrEmpty(text)) return;
        lock (logLock)
        {
            try { File.AppendAllText(logFile, DateTime.Now.ToString("s") + " " + text + Environment.NewLine, Encoding.UTF8); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    private Process RunScript(string file, string arguments)
    {
        var info = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe"));
        info.Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(Path.Combine(root, file)) + " " + arguments;
        info.WorkingDirectory = root;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        info.EnvironmentVariables["CS2_LAUNCHER_STOP_FILE"] = stopFile;
        var process = new Process { StartInfo = info };
        process.OutputDataReceived += (sender, args) => Log(args.Data);
        process.ErrorDataReceived += (sender, args) => Log(args.Data);
        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return process;
    }

    private Dictionary<string, object> ReadState(string name)
    {
        try { return json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(root, ".run", name), Encoding.UTF8)); }
        catch (IOException) { return null; }
        catch (ArgumentException) { return null; }
        catch (InvalidOperationException) { return null; }
    }

    private Process VerifiedSupervisor(Dictionary<string, object> state)
    {
        try
        {
            if (state == null || !String.Equals(Path.GetFullPath((string)state["script_path"]), Path.Combine(root, "start_all.ps1"), StringComparison.OrdinalIgnoreCase)) return null;
            var process = Process.GetProcessById(Convert.ToInt32(state["pid"], CultureInfo.InvariantCulture));
            var birth = DateTime.Parse((string)state["process_started_utc"], CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind).ToUniversalTime();
            if (process.HasExited || Math.Abs((process.StartTime.ToUniversalTime() - birth).TotalSeconds) > 1 ||
                !String.Equals(process.MainModule.FileName, (string)state["executable"], StringComparison.OrdinalIgnoreCase))
            { process.Dispose(); return null; }
            return process;
        }
        catch (Exception) { return null; }
    }

    public void Start()
    {
        if (started && supervisor != null && !supervisor.HasExited) return;
        if (supervisor != null) { supervisor.Dispose(); supervisor = null; }
        started = false;
        instance = null;
        // A failed or externally completed stop can leave this window alive.
        // Its old stop marker must not cancel the next supervisor immediately.
        File.Delete(stopFile);
        var state = ReadState("start_all.windows.json");
        supervisor = VerifiedSupervisor(state);
        if (supervisor != null) instance = (string)state["instance_id"];
        else supervisor = RunScript("start_all.ps1", "-OpenBrowser 0 -QuietDashboardUrl 1");
        started = true;
    }

    public bool HasExited { get { return started && supervisor != null && supervisor.HasExited; } }

    private Dictionary<string, object> ReadyState()
    {
        if (!started || HasExited) return null;
        var state = ReadState("start_all.windows.json");
        if (state == null || Convert.ToInt32(state["pid"], CultureInfo.InvariantCulture) != supervisor.Id) return null;
        instance = (string)state["instance_id"];
        var ready = ReadState("launcher.ready.json");
        return ready != null && (string)ready["instance_id"] == instance ? ready : null;
    }

    public bool IsReady { get { return ReadyState() != null; } }

    public void OpenSite()
    {
        var ready = ReadyState();
        if (ready == null) return;
        Uri url;
        if (!Uri.TryCreate((string)ready["url"], UriKind.Absolute, out url) ||
            (url.Scheme != "http" && url.Scheme != "https") || !url.IsLoopback)
            throw new InvalidOperationException("Некорректный локальный адрес сайта.");
        string token = File.ReadAllText((string)ready["token_path"], Encoding.UTF8).Trim();
        // The private URL stays out of launcher state, shortcuts and logs.
        Process.Start(new ProcessStartInfo(url.AbsoluteUri + "?token=" + Uri.EscapeDataString(token)) { UseShellExecute = true });
    }

    public void OpenLogs()
    {
        Process.Start(new ProcessStartInfo(Path.Combine(root, "logs")) { UseShellExecute = true });
    }

    public void RequestStop()
    {
        // This marker also covers closing before the supervisor publishes its PID.
        File.WriteAllText(stopFile, "stop", Encoding.ASCII);
        // An adopted supervisor may have been launched from a different window.
        // Notify its verified instance directly; do not depend on starting a new
        // PowerShell process while Windows is already ending the user session.
        var state = ReadState("start_all.windows.json");
        using (var owner = VerifiedSupervisor(state))
        {
            if (owner == null || supervisor == null || owner.Id != supervisor.Id) return;
            Guid ownerId;
            if (!Guid.TryParseExact((string)state["instance_id"], "N", out ownerId)) return;
            var request = new Dictionary<string, object> {
                { "schema_version", 1 }, { "instance_id", ownerId.ToString("N") },
                { "requested_utc", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) }
            };
            string destination = Path.Combine(root, ".run", "stop_all.windows.request");
            string temporary = destination + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, json.Serialize(request), Encoding.UTF8);
                if (File.Exists(destination)) File.Replace(temporary, destination, null);
                else File.Move(temporary, destination);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
    }

    public void Stop()
    {
        try { StopCore(); }
        catch (Exception error) { Log("Stop failed: " + error.Message); throw; }
    }

    private void StopCore()
    {
        RequestStop();
        if (!started) return;
        var deadline = DateTime.UtcNow.AddSeconds(120);
        // Let an already launching supervisor either publish its state or observe
        // our marker. Do not report success while it could still start services.
        while (!HasExited && DateTime.UtcNow < deadline)
        {
            var state = ReadState("start_all.windows.json");
            if (state != null && Convert.ToInt32(state["pid"], CultureInfo.InvariantCulture) == supervisor.Id) break;
            Thread.Sleep(100);
        }
        using (var stop = RunScript("stop_all.ps1", "-TimeoutSeconds 110"))
        {
            int remaining = Math.Max(1, (int)(deadline - DateTime.UtcNow).TotalMilliseconds);
            if (!stop.WaitForExit(remaining)) throw new TimeoutException("Остановка ещё продолжается. Проверьте журнал launcher.log.");
            stop.WaitForExit();
            if (stop.ExitCode != 0) throw new InvalidOperationException("Не удалось завершить все процессы. Подробности в launcher.log.");
        }
        if (!supervisor.WaitForExit(Math.Max(1, (int)(deadline - DateTime.UtcNow).TotalMilliseconds)))
            throw new TimeoutException("Ожидание завершения запуска. Подробности в launcher.log.");
        File.Delete(stopFile);
    }

    public void Dispose()
    {
        if (supervisor != null) supervisor.Dispose();
    }
}

internal sealed class LauncherWindow : Form
{
    private readonly ILauncherRuntime runtime;
    private readonly bool autoStart;
    private readonly uint activateMessage;
    private readonly EventWaitHandle activationSignal;
    private readonly Label status;
    private readonly Button open, stop;
    private readonly NotifyIcon tray;
    private readonly System.Windows.Forms.Timer timer;
    private Task stopTask;
    private bool allowClose, openWhenReady, exitShown, startFailed;

    internal LauncherWindow(ILauncherRuntime backend, bool background, uint activation, EventWaitHandle pendingActivation = null)
    {
        runtime = backend;
        autoStart = background;
        openWhenReady = !background;
        activateMessage = activation;
        activationSignal = pendingActivation;
        Text = "CS2 Dashboard";
        ClientSize = new Size(520, 205);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10);
        BackColor = Color.FromArgb(24, 28, 36);
        ForeColor = Color.WhiteSmoke;
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { }
        var title = new Label { Text = "CS2 Dashboard", Font = new Font("Segoe UI", 18, FontStyle.Bold), Location = new Point(20, 18), AutoSize = true };
        status = new Label { Text = "Запускаю сервисы…", Location = new Point(22, 65), Size = new Size(475, 48) };
        var hint = new Label { Text = "Крестик — остановить. Свернуть — оставить работать в фоне.", Location = new Point(22, 167), AutoSize = true, ForeColor = Color.Silver, Font = new Font("Segoe UI", 9) };
        open = MakeButton("Открыть сайт", 22, (sender, args) => OpenSite());
        open.Enabled = false;
        var logs = MakeButton("Журналы", 178, (sender, args) => OpenLogs());
        stop = MakeButton("Остановить", 334, (sender, args) => Close());
        Controls.AddRange(new Control[] { title, status, open, logs, stop, hint });
        var menu = new ContextMenuStrip();
        menu.Items.Add("Открыть управление", null, (sender, args) => ActivateWindow());
        menu.Items.Add("Открыть сайт", null, (sender, args) => OpenSite());
        menu.Items.Add("Остановить", null, (sender, args) => Close());
        tray = new NotifyIcon { Icon = Icon, Text = "CS2 Dashboard", Visible = true, ContextMenuStrip = menu };
        tray.DoubleClick += (sender, args) => ActivateWindow();
        timer = new System.Windows.Forms.Timer { Interval = 500 };
        timer.Tick += (sender, args) =>
        {
            if (activationSignal != null && activationSignal.WaitOne(0)) ActivateWindow();
            RefreshStatus();
        };
        Resize += (sender, args) => { if (WindowState == FormWindowState.Minimized) { Hide(); ShowInTaskbar = false; } };
        FormClosing += OnClosing;
        Shown += (sender, args) =>
        {
            try { runtime.Start(); startFailed = false; timer.Start(); }
            catch (Exception error)
            {
                startFailed = true;
                status.Text = "Ошибка запуска: " + error.Message;
                tray.Text = "CS2 Dashboard — ошибка запуска";
                timer.Start();
                return;
            }
            if (autoStart) { Hide(); ShowInTaskbar = false; }
        };
    }

    private Button MakeButton(string text, int x, EventHandler action)
    {
        var button = new Button { Text = text, Location = new Point(x, 121), Size = new Size(146, 34), FlatStyle = FlatStyle.Flat, BackColor = Color.FromArgb(43, 52, 67) };
        button.Click += action;
        return button;
    }

    internal void ActivateWindow()
    {
        ShowInTaskbar = true;
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
        try
        {
            if (stopTask != null) return;
            if (runtime.HasExited || startFailed)
            {
                startFailed = true;
                runtime.Start();
                startFailed = false;
                exitShown = false;
                open.Enabled = false;
                status.Text = "Запускаю сервисы…";
                tray.Text = "CS2 Dashboard";
            }
            timer.Start();
            if (runtime.IsReady) OpenSite(); else openWhenReady = true;
        }
        catch (Exception error) { status.Text = "Не удалось прочитать состояние: " + error.Message; }
    }

    private void OpenSite()
    {
        try { runtime.OpenSite(); }
        catch (Exception error) { status.Text = "Не удалось открыть сайт: " + error.Message; }
    }

    private void OpenLogs()
    {
        try { runtime.OpenLogs(); }
        catch (Exception error) { status.Text = "Не удалось открыть журналы: " + error.Message; }
    }

    private void RefreshStatus()
    {
        if (stopTask != null) return;
        try
        {
            if (runtime.HasExited)
            {
                status.Text = "Сервисы остановлены. Подробности — в журналах.";
                open.Enabled = false;
                tray.Text = "CS2 Dashboard — остановлен";
                if (!exitShown)
                {
                    exitShown = true;
                    ShowInTaskbar = true;
                    Show();
                }
                return;
            }
            if (!runtime.IsReady) return;
            open.Enabled = true;
            status.Text = "Работает. Сайт и фоновые сервисы запущены.";
            tray.Text = "CS2 Dashboard — работает";
            if (openWhenReady) { openWhenReady = false; OpenSite(); }
        }
        catch (Exception) { /* Atomic state may be replaced between polls. */ }
    }

    private Task BeginStop()
    {
        if (stopTask != null) return stopTask;
        timer.Stop();
        status.Text = "Завершаю работу сервисов. Подождите…";
        open.Enabled = false;
        stop.Enabled = false;
        tray.Text = "CS2 Dashboard — завершение";
        Native.ShutdownBlockReasonCreate(Handle, "CS2 завершает работу сервисов и базы данных.");
        runtime.RequestStop();
        stopTask = Task.Run((Action)runtime.Stop);
        return stopTask;
    }

    private async void OnClosing(object sender, FormClosingEventArgs args)
    {
        if (allowClose) return;
        args.Cancel = true;
        if (stopTask != null) return;
        try
        {
            await BeginStop();
            allowClose = true;
            Native.ShutdownBlockReasonDestroy(Handle);
            Close();
        }
        catch (Exception error)
        {
            stopTask = null;
            stop.Enabled = true;
            timer.Start();
            status.Text = error.Message;
            Native.ShutdownBlockReasonDestroy(Handle);
            ShowInTaskbar = true;
            Show();
        }
    }

    protected override void WndProc(ref Message message)
    {
        if ((uint)message.Msg == activateMessage)
        {
            ActivateWindow();
            message.Result = IntPtr.Zero;
            return;
        }
        if (message.Msg == Native.QueryEndSession)
        {
            // Consent immediately. A cancelled Windows logout must not stop CS2.
            Native.ShutdownBlockReasonCreate(Handle, "CS2 завершает работу сервисов и базы данных.");
            message.Result = new IntPtr(1);
            return;
        }
        if (message.Msg == Native.EndSession)
        {
            if (message.WParam != IntPtr.Zero)
            {
                try { BeginStop().Wait(TimeSpan.FromSeconds(120)); }
                catch (Exception) { /* The stop script records failures in launcher.log. */ }
                allowClose = true;
            }
            Native.ShutdownBlockReasonDestroy(Handle);
            message.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref message);
    }

    internal IntPtr SimulateSessionMessage(int id, bool ending)
    {
        var message = Message.Create(Handle, id, ending ? new IntPtr(1) : IntPtr.Zero, IntPtr.Zero);
        WndProc(ref message);
        return message.Result;
    }

    internal bool StopComplete { get { return stopTask != null && stopTask.Status == TaskStatus.RanToCompletion; } }
    internal bool StopPending { get { return stopTask != null; } }
    internal bool ActivationPollingActive { get { return timer.Enabled; } }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { timer.Dispose(); tray.Dispose(); runtime.Dispose(); }
        base.Dispose(disposing);
    }
}

internal static class Program
{
    private sealed class FakeRuntime : ILauncherRuntime
    {
        public int Stops, Starts;
        public bool Finished, Exited, FailStart, FailStop;
        public void Start()
        {
            Starts++;
            if (FailStart) { FailStart = false; throw new Exception("test start failure"); }
            Exited = false;
        }
        public void RequestStop() { }
        public void Stop()
        {
            Stops++; Thread.Sleep(100);
            if (FailStop) { FailStop = false; Exited = true; throw new Exception("test stop failure"); }
            Finished = true;
        }
        public bool IsReady { get { return false; } }
        public bool HasExited { get { return Exited; } }
        public void OpenSite() { }
        public void OpenLogs() { }
        public void Dispose() { }
    }

    private static void SelfTest(string resultFile)
    {
        var fake = new FakeRuntime();
        using (var window = new LauncherWindow(fake, true, UInt32.MaxValue))
        {
            if (window.SimulateSessionMessage(Native.QueryEndSession, false) != new IntPtr(1)) throw new Exception("query must consent");
            window.SimulateSessionMessage(Native.EndSession, false);
            if (fake.Stops != 0) throw new Exception("cancelled shutdown stopped services");
            window.SimulateSessionMessage(Native.EndSession, true);
            if (!fake.Finished || !window.StopComplete || fake.Stops != 1) throw new Exception("shutdown returned before cleanup");
            window.SimulateSessionMessage(Native.EndSession, true);
            if (fake.Stops != 1) throw new Exception("duplicate cleanup");
        }
        fake = new FakeRuntime();
        fake.FailStart = true;
        using (var window = new LauncherWindow(fake, true, UInt32.MaxValue))
        {
            window.Show();
            Application.DoEvents();
            window.ActivateWindow();
            if (fake.Starts != 2) throw new Exception("activation did not retry failed startup");
        }
        fake = new FakeRuntime();
        fake.FailStop = true;
        using (var window = new LauncherWindow(fake, true, UInt32.MaxValue))
        {
            window.Show();
            Application.DoEvents();
            window.ActivateWindow();
            // This harness pumps forms without Application.Run. Keep async
            // FormClosing continuations on its UI thread, as in the real app.
            SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
            window.Close();
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while ((fake.Stops == 0 || window.StopPending) && DateTime.UtcNow < deadline)
            { Application.DoEvents(); Thread.Sleep(10); }
            if (window.IsDisposed || window.StopPending || fake.Stops != 1) throw new Exception("failed stop did not leave a recoverable window");
            if (!window.ActivationPollingActive) throw new Exception("failed stop disabled shortcut activation polling");
            window.ActivateWindow();
            if (fake.Starts != 2) throw new Exception("activation did not recover after failed stop");
        }
        fake = new FakeRuntime();
        using (var window = new LauncherWindow(fake, true, UInt32.MaxValue))
        {
            window.Show();
            Application.DoEvents();
            window.ActivateWindow();
            if (fake.Starts != 1) throw new Exception("activation duplicated a running supervisor");
            fake.Exited = true;
            window.ActivateWindow();
            if (fake.Starts != 2 || fake.Exited) throw new Exception("activation did not restart stopped services");
            window.ActivateWindow();
            if (fake.Starts != 2) throw new Exception("repeated activation duplicated restarted services");
        }
        fake = new FakeRuntime();
        using (var window = new LauncherWindow(fake, true, UInt32.MaxValue))
        {
            window.Show();
            window.Close();
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while (!window.IsDisposed && DateTime.UtcNow < deadline) { Application.DoEvents(); Thread.Sleep(10); }
            if (!window.IsDisposed || !fake.Finished || fake.Stops != 1 || fake.Starts != 1) throw new Exception("window close did not await cleanup");
        }
        File.WriteAllText(resultFile, "PASS: stopped services restart; failed startup retries; failed stop recovers activation; running services are reused; close waits; shutdown waits; cancelled shutdown preserves services; repeated shutdown is idempotent.", Encoding.UTF8);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        if (args.Length == 2 && args[0] == "--self-test")
        {
            try { SelfTest(args[1]); return 0; }
            catch (Exception error) { File.WriteAllText(args[1], error.ToString()); return 1; }
        }
        string root = Path.GetFullPath(args[0]);
        bool automatic = args.Length > 1 && args[1] == "--autostart";
        string key;
        using (var sha = SHA256.Create()) { key = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant()))).Replace("-", "").Substring(0, 24); }
        uint activation = Native.RegisterWindowMessage("CS2Launcher.Activate." + key);
        bool created;
        using (var activationSignal = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\CS2Launcher.Activate." + key))
        using (var singleton = new Mutex(true, @"Local\CS2Launcher." + key, out created))
        {
            if (!created)
            {
                // The signal survives a click before the first window exists.
                if (!automatic) activationSignal.Set();
                return 0;
            }
            Native.SetProcessShutdownParameters(0x3ff, 0);
            try { Application.Run(new LauncherWindow(new LauncherRuntime(root), automatic, activation, activationSignal)); }
            finally { singleton.ReleaseMutex(); }
        }
        return 0;
    }
}
