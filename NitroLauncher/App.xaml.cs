using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Threading;

namespace NitroLauncher;

public partial class App : Application
{
    static Mutex _singleton;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        ShutdownMode = ShutdownMode.OnMainWindowClose;

        bool created;
        _singleton = new Mutex(true, @"Local\MarioKartNitro.Launcher.Singleton", out created);
        if (!created)
        {
            // Already running: bring that window to the front and leave quietly.
            NativeMethods.FocusExisting("Mario Kart Nitro — Launcher");
            Shutdown();
            return;
        }

        DispatcherUnhandledException += OnUnhandled;
        Tr.Init();
        var w = new MainWindow();
        MainWindow = w;
        w.Show();
    }

    static void OnUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        try
        {
            File.AppendAllText(Path.Combine(Paths.AppData, "launcher_error.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " | " + e.Exception + "\n", new UTF8Encoding(false));
        }
        catch { }
        e.Handled = true; // never take the whole launcher down over one UI hiccup
    }
}

static class NativeMethods
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr FindWindow(string className, string windowName);

    [DllImport("user32.dll")]
    static extern bool ShowWindow(IntPtr hWnd, int cmd);

    [DllImport("user32.dll")]
    static extern bool SetForegroundWindow(IntPtr hWnd);

    public static void FocusExisting(string title)
    {
        try
        {
            IntPtr h = FindWindow(null, title);
            if (h != IntPtr.Zero)
            {
                ShowWindow(h, 9); // SW_RESTORE
                SetForegroundWindow(h);
            }
        }
        catch { }
    }
}
