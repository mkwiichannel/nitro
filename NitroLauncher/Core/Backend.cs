using System.Diagnostics;
using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;

namespace NitroLauncher;

public class ProgressInfo
{
    public string Status = "idle"; // idle | verifying | downloading | extracting | done | error
    public long Downloaded;
    public long? Total;
    public string Error;
    public string Version;
}

public static class Progress
{
    static readonly object L = new object();
    static readonly ProgressInfo P = new ProgressInfo();

    public static void Set(string status = null, long? downloaded = null, long? total = null, bool setTotal = false,
        string error = null, bool setError = false, string version = null)
    {
        lock (L)
        {
            if (status != null) P.Status = status;
            if (downloaded.HasValue) P.Downloaded = downloaded.Value;
            if (setTotal) P.Total = total;
            if (setError) P.Error = error;
            if (version != null) P.Version = version;
        }
    }

    public static ProgressInfo Snapshot()
    {
        lock (L)
        {
            return new ProgressInfo
            {
                Status = P.Status, Downloaded = P.Downloaded, Total = P.Total, Error = P.Error, Version = P.Version,
            };
        }
    }
}

public class CheckResult
{
    public bool UpdateAvailable;
    public bool IsFirstInstall;
    public string LatestVersion = "";
    public string CurrentVersion = "";
    public string DownloadUrl = "";
    public string Error;
    public bool LauncherUpdateAvailable;
    public string LauncherDownloadUrl = "";
}

public static class Backend
{
    public const string AppVersion = "1.0.0";
    public const string DiscordUrl = "https://discord.com/invite/wbU8vw8vJq";
    public const string ManifestUrl = "https://raw.githubusercontent.com/mkwiichannel/nitro/main/manifest.json";

    /// <summary>Raised (on a worker thread) when theme data in config.json changed.</summary>
    public static event Action StateChanged;

    class Pending
    {
        public string Repo, Branch, Dest;
        public RemoteInfo Remote;
        public Plan Plan;
        public SyncState State;
    }

    static Pending _pending;

    // ------------------------------------------------------------ state for the UI / Mii page
    public static JsonObject GetState()
    {
        var cfg = Cfg.Load();
        cfg["version"] = AppVersion;
        cfg["build_stamp"] = "v" + AppVersion;
        cfg["discord_url"] = DiscordUrl;
        return cfg;
    }

    public static void SaveSettings(JsonObject payload)
    {
        var cfg = Cfg.Load();
        foreach (var key in new[] { "dolphin_path", "iso_path", "mod_directory", "resolution", "language",
                                    "fullscreen", "auto_update", "ffl_resource_path" })
        {
            if (payload.ContainsKey(key) && payload[key] != null)
                cfg[key] = payload[key].DeepClone();
        }
        Cfg.Save(cfg);
    }

    public static void OpenDiscord()
    {
        try { Process.Start(new ProcessStartInfo(DiscordUrl) { UseShellExecute = true }); } catch { }
    }

    // ------------------------------------------------------------ remote theme (colors / logo / banner)
    static bool DownloadAndCache(JsonObject cfg, string url, string urlKey, string pathKey, string cacheName)
    {
        url = (url ?? "").Trim();
        if (url == "") return false;
        try
        {
            byte[] data = Net.FetchBytes(url, 20);
            string ext = ".bin";
            try
            {
                string e = Path.GetExtension(new Uri(url).AbsolutePath);
                if (!string.IsNullOrEmpty(e)) ext = e;
            }
            catch { }
            string path = Path.Combine(Paths.AppData, cacheName + ext);
            byte[] existing = null;
            if (File.Exists(path))
            {
                try { existing = File.ReadAllBytes(path); } catch { }
            }
            if (existing != null && existing.AsSpan().SequenceEqual(data) && cfg.Str(urlKey) == url)
                return false; // nothing actually changed
            File.WriteAllBytes(path, data);
            cfg[urlKey] = url;
            cfg[pathKey] = path;
            return true;
        }
        catch { return false; } // keep whatever was cached before
    }

    static bool DownloadOrClear(JsonObject cfg, string url, string urlKey, string pathKey, string cacheName)
    {
        url = (url ?? "").Trim();
        if (url != "") return DownloadAndCache(cfg, url, urlKey, pathKey, cacheName);
        bool had = cfg.Str(urlKey) != "" || cfg.Str(pathKey) != "";
        if (!had) return false;
        string old = cfg.Str(pathKey);
        if (old != "" && File.Exists(old))
        {
            try { File.Delete(old); } catch { }
        }
        cfg[urlKey] = "";
        cfg[pathKey] = "";
        return true;
    }

    /// <summary>Applies the optional "theme" block of manifest.json (colors, logo, banner).
    /// Anything the manifest stops providing falls back to the built-in default.</summary>
    static bool ApplyTheme(JsonObject manifest, JsonObject cfg)
    {
        var theme = manifest.ObjOrNull("theme") ?? new JsonObject();
        bool changed = false;

        string season = theme.Str("season").Trim();
        if (season != cfg.Str("theme_season")) { cfg["theme_season"] = season; changed = true; }

        var clean = new JsonObject();
        if (theme["colors"] is JsonObject colors)
        {
            foreach (var kv in colors)
            {
                if (kv.Key.StartsWith("--") && kv.Value != null)
                {
                    try { clean[kv.Key] = kv.Value.GetValue<string>(); } catch { }
                }
            }
        }
        string curColors = (cfg["theme_colors"] as JsonObject ?? new JsonObject()).ToJsonString();
        if (clean.ToJsonString() != curColors) { cfg["theme_colors"] = clean; changed = true; }

        if (DownloadOrClear(cfg, theme.Str("logo_url"), "theme_logo_url", "theme_logo_path", "theme_logo")) changed = true;
        if (DownloadOrClear(cfg, theme.Str("banner_url"), "theme_banner_url", "theme_banner_path", "theme_banner")) changed = true;

        if (changed)
        {
            Cfg.Save(cfg);
            try { StateChanged?.Invoke(); } catch { }
        }
        return changed;
    }

    static JsonObject FetchManifest(int timeoutSeconds)
    {
        var text = Encoding.UTF8.GetString(Net.FetchBytes(ManifestUrl, timeoutSeconds));
        var o = JsonNode.Parse(text) as JsonObject;
        if (o == null) throw new InvalidDataException("manifest.json is not a JSON object");
        return o;
    }

    /// <summary>Keeps colors/logo/banner current while the app stays open.</summary>
    public static void StartBackgroundThemeLoop()
    {
        var t = new Thread(() =>
        {
            bool first = true;
            while (true)
            {
                Thread.Sleep(first ? 5000 : 120000);
                first = false;
                try
                {
                    var manifest = FetchManifest(10);
                    ApplyTheme(manifest, Cfg.Load());
                }
                catch { /* offline this cycle: try again next interval */ }
            }
        });
        t.IsBackground = true;
        t.Start();
    }

    // ------------------------------------------------------------ update check
    public static CheckResult CheckForUpdate(bool light)
    {
        JsonObject manifest;
        try { manifest = FetchManifest(10); }
        catch (Exception e) { return new CheckResult { Error = "Couldn't check for updates: " + e.Message }; }

        var cfg = Cfg.Load();
        ApplyTheme(manifest, cfg);
        cfg = Cfg.Load();

        // The whole-exe self-update is checked first and independently of the modpack.
        string launcherUrl = manifest.Str("launcher_nitro").Trim();
        string installedUrl = cfg.Str("installed_launcher_url");
        if (launcherUrl != "" && installedUrl == "" && !cfg.Bool("_launcher_baseline_set"))
        {
            // First check ever on this machine: whatever is running IS this build, so adopt the
            // current value as the baseline instead of nagging a fresh download with "Update!".
            cfg["installed_launcher_url"] = launcherUrl;
            cfg["_launcher_baseline_set"] = true;
            Cfg.Save(cfg);
            installedUrl = launcherUrl;
        }
        var result = new CheckResult
        {
            LauncherUpdateAvailable = launcherUrl != "" && launcherUrl != installedUrl,
            LauncherDownloadUrl = launcherUrl,
        };
        if (light) return result; // opening the app stays light: the modpack scan happens on Play

        string repo = manifest.Str("modpack_repo").Trim();
        if (repo == "") repo = ModpackSync.DefaultRepo;
        string branch = manifest.Str("modpack_branch").Trim();
        if (branch == "") branch = ModpackSync.DefaultBranch;

        string dolphin = cfg.Str("dolphin_path");
        if (dolphin == "" || !File.Exists(dolphin))
        {
            result.Error = "Set your Dolphin path in Settings first - the modpack installs into Dolphin's folder.";
            return result;
        }

        string dest = Game.RiivolutionRoot(dolphin);
        var state = ModpackSync.LoadState(Paths.ModpackState);
        RemoteInfo remote;
        Plan plan;
        try
        {
            remote = ModpackSync.FetchRemote(repo, branch, state);
            ModpackSync.ResolveLfs(repo, branch, remote);
            plan = ModpackSync.MakePlan(dest, remote.Files, state,
                (done, total) => Progress.Set(status: "verifying", downloaded: done, total: total > 0 ? total : (long?)null, setTotal: true));
            Progress.Set(status: "idle", downloaded: 0, total: null, setTotal: true);
            if (plan.Download.Count == 0 && plan.Delete.Count == 0 && state.Files.Count > 0)
            {
                try { ModpackSync.RememberVerified(repo, branch, dest, remote, plan, state, Paths.ModpackState); }
                catch (IOException) { }
            }
        }
        catch (SyncException e)
        {
            result.Error = e.Message;
            return result;
        }
        catch (Exception e)
        {
            result.Error = "Couldn't read Dolphin's folder: " + e.Message;
            return result;
        }

        _pending = new Pending { Repo = repo, Branch = branch, Dest = dest, Remote = remote, Plan = plan, State = state };
        bool installedBefore = state.Files.Count > 0 && state.Dest == ModpackSync.NormDest(dest);
        string latest = remote.TreeSha.Length >= 7 ? remote.TreeSha.Substring(0, 7) : remote.TreeSha;
        string current = state.TreeSha.Length >= 7 ? state.TreeSha.Substring(0, 7) : state.TreeSha;
        result.UpdateAvailable = plan.Download.Count > 0 || plan.Delete.Count > 0;
        result.IsFirstInstall = !installedBefore;
        result.LatestVersion = latest;
        result.CurrentVersion = current == "" ? "0" : current;
        result.DownloadUrl = $"repo:{repo}@{branch}";
        return result;
    }

    // ------------------------------------------------------------ modpack download
    public static void StartUpdate(string downloadUrl, string latestVersion)
    {
        Progress.Set(status: "downloading", downloaded: 0, total: null, setTotal: true,
            error: null, setError: true, version: latestVersion);
        var t = new Thread(() => ModpackWorker(downloadUrl));
        t.IsBackground = true;
        t.Start();
    }

    static void ModpackWorker(string downloadUrl)
    {
        try
        {
            var pend = _pending;
            if (pend == null)
            {
                Progress.Set(status: "error", error: "Modpack check hasn't run yet - press Play again.", setError: true);
                return;
            }
            var cfg = Cfg.Load();
            if (pend.Plan.Download.Count > 0 || pend.Plan.Delete.Count > 0)
                Game.KillDolphin(cfg.Str("dolphin_path")); // Windows can't overwrite open files

            // One-time migration from the old zip-based install: remember which top-level
            // folders it created (they're stale now).
            string oldContent = Path.Combine(Paths.AppData, "mods", "Nitro Pack", "content");
            bool firstTime = pend.State.Files.Count == 0;
            var staleDirs = new List<string>();
            if (firstTime && Directory.Exists(oldContent))
            {
                var tops = new HashSet<string>(pend.Remote.Files.Keys.Select(p => p.Split('/')[0]));
                try
                {
                    foreach (var d in Directory.GetDirectories(oldContent))
                    {
                        string name = Path.GetFileName(d);
                        if (!tops.Contains(name)) staleDirs.Add(name);
                    }
                }
                catch { staleDirs.Clear(); }
            }

            ModpackSync.RunSync(pend.Repo, pend.Branch, pend.Dest, pend.Remote, pend.Plan, pend.State,
                Paths.ModpackState,
                (done, total) => Progress.Set(status: "downloading", downloaded: done,
                    total: total > 0 ? total : (long?)null, setTotal: true));
            Progress.Set(status: "extracting"); // finishing up: config + cleanup

            // Point the Nitro Pack entry at the synced files.
            var xmls = pend.Remote.Files.Keys
                .Where(p => p.EndsWith(".xml", StringComparison.OrdinalIgnoreCase))
                .OrderBy(p => p, StringComparer.Ordinal).ToList();
            var topXml = xmls.Where(p => p.StartsWith("riivolution/", StringComparison.OrdinalIgnoreCase)
                                         && p.Count(c => c == '/') == 1).ToList();
            string chosen = topXml.Count > 0 ? topXml[0] : (xmls.Count > 0 ? xmls[0] : "");

            cfg = Cfg.Load();
            string tree = pend.Remote.TreeSha;
            cfg["content_version"] = tree.Length >= 7 ? tree.Substring(0, 7) : (cfg.Str("content_version") == "" ? "0" : cfg.Str("content_version"));
            cfg["installed_from_url"] = downloadUrl;
            cfg["modpack_updated_at"] = DateTimeOffset.Now.ToString("yyyy-MM-ddTHH:mm:sszzz");
            if (cfg["mods"] is JsonArray mods)
            {
                foreach (var n in mods)
                {
                    if (n is JsonObject m && m.Str("name") == "Nitro Pack")
                    {
                        m["content_root"] = pend.Dest;
                        if (chosen != "") m["xml_path"] = ModpackSync.LocalPath(pend.Dest, chosen);
                    }
                }
            }
            Cfg.Save(cfg);

            // Old-format leftovers (frees ~1.6 GB). Best effort.
            foreach (var d in staleDirs)
            {
                try { Directory.Delete(Path.Combine(pend.Dest, d), true); } catch { }
            }
            if (firstTime && Directory.Exists(oldContent))
            {
                try { Directory.Delete(oldContent, true); } catch { }
            }
            Progress.Set(status: "done", version: cfg.Str("content_version"));
            try { StateChanged?.Invoke(); } catch { }
        }
        catch (SyncException e)
        {
            Progress.Set(status: "error", error: e.Message, setError: true);
        }
        catch (Exception e)
        {
            Progress.Set(status: "error", error: "Modpack update failed: " + e.Message, setError: true);
        }
    }

    // ------------------------------------------------------------ whole-launcher self-update
    /// <summary>Downloads the new launcher exe (manifest.json "launcher_nitro", a bare exe or a zip),
    /// stages a small cmd helper that swaps it in once this process has exited, and calls
    /// onQuit. Errors before that point go to onError so the popup can show them.</summary>
    public static void StartLauncherUpdate(string url, Action<string> onError, Action onQuit)
    {
        url = (url ?? "").Trim();
        if (url == "") { onError("No launcher download URL given."); return; }
        string exe = Environment.ProcessPath ?? "";
        if (exe == "" || !exe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)
            || Path.GetFileNameWithoutExtension(exe).Equals("dotnet", StringComparison.OrdinalIgnoreCase))
        {
            onError("Launcher self-update only works in the installed .exe, not a dev run.");
            return;
        }
        var t = new Thread(() => LauncherWorker(url, exe, onError, onQuit));
        t.IsBackground = true;
        t.Start();
    }

    static void LauncherWorker(string url, string exePath, Action<string> onError, Action onQuit)
    {
        string tmpDownload = Path.Combine(Paths.AppData, "_launcher_update.download");
        string tmpExtract = Path.Combine(Paths.AppData, "_launcher_extract");
        string newExe;
        try
        {
            Net.DownloadFile(url, tmpDownload, null);
        }
        catch (Exception e) { onError("Download failed: " + e.Message); return; }

        bool isZip = false;
        try
        {
            using var fs = File.OpenRead(tmpDownload);
            var magic = new byte[4];
            isZip = fs.Read(magic, 0, 4) == 4 && magic[0] == 0x50 && magic[1] == 0x4B && magic[2] == 3 && magic[3] == 4;
        }
        catch { }

        if (isZip)
        {
            try
            {
                if (Directory.Exists(tmpExtract)) Directory.Delete(tmpExtract, true);
                Directory.CreateDirectory(tmpExtract);
                ZipFile.ExtractToDirectory(tmpDownload, tmpExtract);
            }
            catch (Exception e) { onError("Couldn't unpack the update: " + e.Message); return; }
            finally { try { File.Delete(tmpDownload); } catch { } }

            newExe = null;
            string want = Path.GetFileName(exePath);
            var all = Directory.GetFiles(tmpExtract, "*.exe", SearchOption.AllDirectories);
            foreach (var f in all)
                if (Path.GetFileName(f).Equals(want, StringComparison.OrdinalIgnoreCase)) { newExe = f; break; }
            if (newExe == null && all.Length > 0) newExe = all[0];
            if (newExe == null) { onError("The downloaded update doesn't contain an .exe."); return; }
        }
        else
        {
            newExe = Path.Combine(Paths.AppData, "_launcher_update_new.exe");
            try
            {
                if (File.Exists(newExe)) File.Delete(newExe);
                File.Move(tmpDownload, newExe);
            }
            catch (Exception e) { onError("Couldn't prepare the downloaded exe: " + e.Message); return; }
        }

        string backup = exePath + ".old.exe";
        string bat = Path.Combine(Paths.AppData, "apply_launcher_update.bat");
        try
        {
            File.WriteAllText(bat,
                "@echo off\r\n" +
                "setlocal\r\n" +
                "set OLDEXE=%~1\r\n" +
                "set NEWEXE=%~2\r\n" +
                "set BACKUP=%~3\r\n" +
                "set EXTRACTDIR=%~4\r\n" +
                "for /l %%i in (1,1,15) do (\r\n" +
                "  move /y \"%OLDEXE%\" \"%BACKUP%\" >nul 2>nul\r\n" +
                "  if exist \"%BACKUP%\" goto moved\r\n" +
                "  timeout /t 2 /nobreak >nul\r\n" +
                ")\r\n" +
                "rem Couldn't safely swap the exe in time -- reopen the old version untouched.\r\n" +
                "start \"\" \"%OLDEXE%\"\r\n" +
                "goto cleanup\r\n" +
                ":moved\r\n" +
                "copy /y \"%NEWEXE%\" \"%OLDEXE%\" >nul\r\n" +
                "del /f /q \"%BACKUP%\" >nul 2>nul\r\n" +
                "start \"\" \"%OLDEXE%\"\r\n" +
                ":cleanup\r\n" +
                "del /f /q \"%NEWEXE%\" >nul 2>nul\r\n" +
                "rmdir /s /q \"%EXTRACTDIR%\" >nul 2>nul\r\n" +
                "endlocal\r\n", new UTF8Encoding(false));
            // The extra outer quotes matter: cmd /c strips the first and last quote of the
            // line, which breaks paths with spaces (e.g. a user name with a space).
            var psi = new ProcessStartInfo("cmd.exe")
            {
                Arguments = $"/c \"\"{bat}\" \"{exePath}\" \"{newExe}\" \"{backup}\" \"{tmpExtract}\"\"",
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            Process.Start(psi);
        }
        catch (Exception e) { onError("Couldn't stage the update: " + e.Message); return; }

        var cfg = Cfg.Load();
        cfg["installed_launcher_url"] = url;
        cfg["launcher_updated_at"] = DateTimeOffset.Now.ToString("yyyy-MM-ddTHH:mm:sszzz");
        Cfg.Save(cfg);

        Thread.Sleep(500); // a short head start so the helper's first attempt can succeed
        try { onQuit(); } catch { }
    }
}
