using System.Text;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Media;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Microsoft.Win32;

namespace NitroLauncher;

/// <summary>The Mii editor (3D renderer, WebGL) is the one part that needs a browser engine, so it
/// lives in its own window that only exists while you are editing Miis. The launcher itself never
/// loads a browser engine. The page is the same index.html as before; the small bridge below
/// answers the calls it used to make to the old Python backend.</summary>
public class MiiWindow : Window
{
    readonly WebView2 _web = new WebView2();

    const string Host = "nitro.local";

    // Gives the page the same window.pywebview.api.* functions the old launcher provided.
    const string Shim = @"
(function(){
  var pending = {}; var n = 0;
  window.chrome.webview.addEventListener('message', function(e){
    var m = e.data; var cb = pending[m.id];
    if(cb){ delete pending[m.id]; cb(m.result); }
  });
  var api = new Proxy({}, { get: function(t, k){
    if(k === 'then' || typeof k !== 'string') return undefined;
    return function(){
      var args = Array.prototype.slice.call(arguments);
      return new Promise(function(res){
        var id = ++n; pending[id] = res;
        window.chrome.webview.postMessage({ id: id, method: k, args: args });
      });
    };
  }});
  window.pywebview = { api: api };
  window.addEventListener('DOMContentLoaded', function(){
    setTimeout(function(){ window.dispatchEvent(new Event('pywebviewready')); }, 0);
  });
})();";

    public MiiWindow()
    {
        Title = "Mario Kart Nitro — Mii Editor";
        Width = 1100;
        Height = 780;
        MinWidth = 900;
        MinHeight = 640;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = new SolidColorBrush(Color.FromRgb(6, 10, 19));
        Content = _web;
        Loaded += async (s, e) => await InitAsync();
    }

    async Task InitAsync()
    {
        try
        {
            var opts = new CoreWebView2EnvironmentOptions("--disable-features=ElasticOverscroll --disable-gpu-compositing");
            var env = await CoreWebView2Environment.CreateAsync(null, Paths.WebView2Data, opts);
            await _web.EnsureCoreWebView2Async(env);
            var core = _web.CoreWebView2;

            string dir = await Task.Run(ExtractWebFiles);
            core.SetVirtualHostNameToFolderMapping(Host, dir, CoreWebView2HostResourceAccessKind.Allow);
            await core.AddScriptToExecuteOnDocumentCreatedAsync(Shim);
            core.WebMessageReceived += OnMessage;
            core.NavigationCompleted += async (s, e) =>
            {
                // Open the Mii page straight away and hide the launcher's own navigation here.
                await core.ExecuteScriptAsync(
                    "go('mii');var n=document.getElementById('tabs');if(n)n.style.display='none';" +
                    "var b=document.getElementById('miiBackBtn');if(b)b.style.display='none';");
            };
            core.Navigate("https://" + Host + "/index.html");
        }
        catch (Exception ex)
        {
            MessageBox.Show("The Mii editor could not start.\n\n" + ex.Message, "Mario Kart Nitro",
                MessageBoxButton.OK, MessageBoxImage.Warning);
            Close();
        }
    }

    /// <summary>Unpacks the editor page + renderer from inside the exe (only when the exe changed).</summary>
    static string ExtractWebFiles()
    {
        string dir = Paths.MiiWeb;
        string marker = Path.Combine(dir, ".build");
        string id = "0";
        try
        {
            var fi = new FileInfo(Environment.ProcessPath ?? "");
            id = fi.Length + "-" + fi.LastWriteTimeUtc.Ticks;
        }
        catch { }
        try
        {
            if (File.Exists(marker) && File.ReadAllText(marker) == id && File.Exists(Path.Combine(dir, "index.html")))
                return dir;
        }
        catch { }

        try { if (Directory.Exists(dir)) Directory.Delete(dir, true); } catch { }
        Directory.CreateDirectory(dir);
        foreach (var name in Res.Names)
        {
            if (!name.StartsWith("web/")) continue;
            string rel = name.Substring(4).Replace('\\', '/');
            string dest = Path.Combine(dir, rel.Replace('/', Path.DirectorySeparatorChar));
            Directory.CreateDirectory(Path.GetDirectoryName(dest));
            using var src = Res.Open(name);
            using var dst = File.Create(dest);
            src.CopyTo(dst);
        }
        File.WriteAllText(marker, id);
        return dir;
    }

    // ------------------------------------------------------------ bridge
    void OnMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        JsonObject msg;
        try { msg = JsonNode.Parse(e.WebMessageAsJson) as JsonObject; }
        catch { return; }
        if (msg == null || msg["id"] == null) return;
        long id = msg["id"].GetValue<long>();
        string method = msg.Str("method");
        var args = msg["args"] as JsonArray ?? new JsonArray();

        Task.Run(() =>
        {
            JsonNode result;
            try { result = Handle(method, args); }
            catch (Exception ex) { result = new JsonObject { ["ok"] = false, ["error"] = ex.Message }; }
            string json = new JsonObject { ["id"] = id, ["result"] = result }.ToJsonString();
            Dispatcher.BeginInvoke(new Action(() =>
            {
                try { _web.CoreWebView2?.PostWebMessageAsJson(json); } catch { }
            }));
        });
    }

    static string ArgStr(JsonArray a, int i)
    {
        try { return a.Count > i && a[i] != null ? a[i].GetValue<string>() : null; } catch { return null; }
    }

    static long ArgLong(JsonArray a, int i, long def)
    {
        try { return a.Count > i && a[i] != null ? a[i].GetValue<long>() : def; } catch { return def; }
    }

    JsonNode Handle(string method, JsonArray args)
    {
        switch (method)
        {
            case "get_state":
                return Backend.GetState();
            case "save_settings":
                if (args.Count > 0 && args[0] is JsonObject payload) Backend.SaveSettings(payload);
                return new JsonObject { ["ok"] = true };
            case "get_miis":
                return MiiStore.GetMiis();
            case "save_mii":
                return MiiStore.SaveMii(ArgLong(args, 0, -1), ArgStr(args, 1), ArgStr(args, 2));
            case "get_ffl_resource_state":
                return MiiStore.FflResourceState();
            case "get_ffl_resource":
                return MiiStore.FflResource();
            case "get_default_mii":
                return MiiStore.DefaultMii();
            case "check_for_update":
                return new JsonObject { ["update_available"] = false, ["launcher_update_available"] = false };
            case "get_download_progress":
                return new JsonObject { ["status"] = "idle", ["downloaded_bytes"] = 0 };
            case "open_discord":
                Backend.OpenDiscord();
                return new JsonObject { ["ok"] = true };
            case "log_ui":
                return JsonValue.Create(true);
            case "quit_app":
                Dispatcher.BeginInvoke(new Action(Close));
                return new JsonObject { ["ok"] = true };
            case "browse_path":
            {
                string kind = ArgStr(args, 0);
                string picked = null;
                Dispatcher.Invoke(new Action(() =>
                {
                    if (kind == "folder")
                    {
                        var d = new OpenFolderDialog();
                        if (d.ShowDialog(this) == true) picked = d.FolderName;
                    }
                    else
                    {
                        var d = new OpenFileDialog();
                        if (d.ShowDialog(this) == true) picked = d.FileName;
                    }
                }));
                return picked == null ? null : JsonValue.Create(picked);
            }
            default:
                return new JsonObject { ["ok"] = false, ["error"] = "Not available in the Mii editor." };
        }
    }
}
