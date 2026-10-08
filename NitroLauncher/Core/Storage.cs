using System.Reflection;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NitroLauncher;

/// <summary>Small helpers for reading loosely-typed JSON (config.json etc.).</summary>
public static class J
{
    public static readonly JsonSerializerOptions Pretty = new JsonSerializerOptions
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string Str(this JsonObject o, string key)
    {
        try
        {
            var n = o[key];
            return n == null ? "" : (n.GetValue<string>() ?? "");
        }
        catch { return ""; }
    }

    public static bool Bool(this JsonObject o, string key, bool def = false)
    {
        try
        {
            var n = o[key];
            return n == null ? def : n.GetValue<bool>();
        }
        catch { return def; }
    }

    public static JsonObject ObjOrNull(this JsonObject o, string key)
    {
        try { return o[key] as JsonObject; } catch { return null; }
    }
}

/// <summary>Per-user data folder: %APPDATA%\MarioKartNitro (same one the old launcher used).</summary>
public static class Paths
{
    public static readonly string AppData = Init();

    static string Init()
    {
        string b = Environment.GetEnvironmentVariable("APPDATA");
        if (string.IsNullOrEmpty(b))
            b = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string p = Path.Combine(b, "MarioKartNitro");
        try
        {
            Directory.CreateDirectory(p);
            Directory.CreateDirectory(Path.Combine(p, "mods"));
        }
        catch { }
        return p;
    }

    public static string Config => Path.Combine(AppData, "config.json");
    public static string ModpackState => Path.Combine(AppData, "modpack_state.json");
    public static string MiiLibrary => Path.Combine(AppData, "mii_library.json");
    public static string Presets => Path.Combine(AppData, "presets");
    public static string MiiWeb => Path.Combine(AppData, "mii_web");
    public static string WebView2Data => Path.Combine(AppData, "webview2_data");
}

/// <summary>Files embedded in the exe.</summary>
public static class Res
{
    static readonly Assembly A = typeof(Res).Assembly;

    public static Stream Open(string name) => A.GetManifestResourceStream(name);

    public static bool Exists(string name)
    {
        foreach (var n in A.GetManifestResourceNames())
            if (n == name) return true;
        return false;
    }

    public static byte[] Bytes(string name)
    {
        using var s = Open(name);
        if (s == null) return null;
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        return ms.ToArray();
    }

    public static string Text(string name)
    {
        var b = Bytes(name);
        return b == null ? "" : new UTF8Encoding(false).GetString(b);
    }

    public static string[] Names => A.GetManifestResourceNames();
}

/// <summary>config.json: same file and keys as the old launcher, so existing settings carry over.</summary>
public static class Cfg
{
    static readonly object L = new object();

    static JsonObject Defaults()
    {
        return new JsonObject
        {
            ["dolphin_path"] = "",
            ["iso_path"] = "",
            ["mod_directory"] = Path.Combine(Paths.AppData, "mods"),
            ["resolution"] = "1920x1080",
            ["language"] = "en",
            ["fullscreen"] = false,
            ["auto_update"] = true,
            ["active_mod"] = "Nitro Pack",
            ["content_version"] = "0.0.1",
            ["installed_from_url"] = "",
            ["installed_launcher_url"] = "",
            ["_launcher_baseline_set"] = false,
            ["theme_season"] = "",
            ["theme_colors"] = new JsonObject(),
            ["theme_banner_url"] = "",
            ["theme_banner_path"] = "",
            ["theme_logo_url"] = "",
            ["theme_logo_path"] = "",
            ["mods"] = new JsonArray
            {
                new JsonObject
                {
                    ["name"] = "Nitro Pack",
                    ["builtin"] = true,
                    ["xml_path"] = "",
                    ["content_root"] = "",
                },
            },
        };
    }

    public static JsonObject Load()
    {
        lock (L)
        {
            var def = Defaults();
            try
            {
                if (File.Exists(Paths.Config))
                {
                    var data = JsonNode.Parse(File.ReadAllText(Paths.Config, Encoding.UTF8)) as JsonObject;
                    if (data != null)
                    {
                        foreach (var kv in def)
                        {
                            if (!data.ContainsKey(kv.Key) && kv.Value != null)
                                data[kv.Key] = kv.Value.DeepClone();
                        }
                        RepairBuiltinMod(data);
                        return data;
                    }
                }
            }
            catch { }
            return def;
        }
    }

    /// <summary>A config from an older build can point the built-in mod at a folder that is gone.</summary>
    static void RepairBuiltinMod(JsonObject cfg)
    {
        try
        {
            string permanent = Path.Combine(Paths.AppData, "mods", "Nitro Pack", "content");
            string goodXml = Path.Combine(permanent, "riivolution", "MKnitro.xml");
            if (cfg["mods"] is not JsonArray mods) return;
            foreach (var node in mods)
            {
                if (node is not JsonObject m || !m.Bool("builtin")) continue;
                string xml = m.Str("xml_path");
                if ((xml == "" || !File.Exists(xml)) && File.Exists(goodXml))
                    m["xml_path"] = goodXml;
                string root = m.Str("content_root");
                if ((root == "" || !Directory.Exists(root)) && Directory.Exists(permanent))
                    m["content_root"] = permanent;
            }
        }
        catch { }
    }

    public static void Save(JsonObject cfg)
    {
        lock (L)
        {
            string tmp = Paths.Config + ".tmp";
            File.WriteAllText(tmp, cfg.ToJsonString(J.Pretty), new UTF8Encoding(false));
            File.Move(tmp, Paths.Config, true);
        }
    }

    public static JsonObject ActiveMod(JsonObject cfg)
    {
        string name = cfg.Str("active_mod");
        if (cfg["mods"] is JsonArray mods)
            foreach (var n in mods)
                if (n is JsonObject m && m.Str("name") == name) return m;
        return null;
    }
}

/// <summary>Translations from translations.json (embedded).</summary>
public static class Tr
{
    static JsonObject _all = new JsonObject();
    public static string Lang = "en";

    public static readonly (string Code, string Name)[] Languages =
    {
        ("en", "English"), ("de", "Deutsch"), ("fr", "Français"),
        ("es", "Español"), ("it", "Italiano"), ("pt", "Português"),
        ("nl", "Nederlands"), ("pl", "Polski"), ("tr", "Türkçe"),
        ("ru", "Русский"), ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"),
        ("ar", "العربية"), ("sv", "Svenska"), ("fi", "Suomi"),
    };

    public static void Init()
    {
        try { _all = JsonNode.Parse(Res.Text("translations.json")) as JsonObject ?? new JsonObject(); }
        catch { _all = new JsonObject(); }
    }

    public static bool Has(string lang) => _all.ContainsKey(lang);

    static string Get(string lang, string key)
    {
        try
        {
            if (_all[lang] is JsonObject o && o[key] != null)
                return o[key].GetValue<string>();
        }
        catch { }
        return null;
    }

    public static string T(string key)
    {
        return Get(Lang, key) ?? Get("en", key) ?? key;
    }
}
