using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NitroLauncher;

public class MiiSyncResult
{
    public bool Ok;
    public int Synced;
    public string Error;
}

/// <summary>Nitro's own Mii library (mii_library.json) and the merge into Dolphin's
/// RFL_DB.dat right before Play. Same file formats and rules as the previous launcher.</summary>
public static class MiiStore
{
    const int CrcOffset = 0x1F1DE;
    const int HeaderOffset = 0x04;
    const int BlockSize = 74;
    const int SlotCount = 100;

    static readonly object L = new object();

    static string DbPath()
    {
        string dolphin = Cfg.Load().Str("dolphin_path");
        if (dolphin == "") return null;
        return Path.Combine(Game.DolphinUserDir(dolphin), "Wii", "shared2", "menu", "FaceLib", "RFL_DB.dat");
    }

    static JsonArray ReadLibrary()
    {
        try
        {
            if (!File.Exists(Paths.MiiLibrary)) return new JsonArray();
            var data = JsonNode.Parse(File.ReadAllText(Paths.MiiLibrary, Encoding.UTF8)) as JsonArray;
            return data ?? new JsonArray();
        }
        catch { return new JsonArray(); }
    }

    static void WriteLibrary(JsonArray items)
    {
        string tmp = Paths.MiiLibrary + ".tmp";
        File.WriteAllText(tmp, items.ToJsonString(J.Pretty), new UTF8Encoding(false));
        File.Move(tmp, Paths.MiiLibrary, true);
    }

    static ushort Crc16(byte[] buf, int length)
    {
        int crc = 0;
        for (int i = 0; i < length; i++)
        {
            crc ^= buf[i] << 8;
            for (int k = 0; k < 8; k++)
                crc = (crc & 0x8000) != 0 ? (((crc << 1) ^ 0x1021) & 0xFFFF) : ((crc << 1) & 0xFFFF);
        }
        return (ushort)crc;
    }

    static string MiiName(byte[] block)
    {
        try
        {
            string raw = Encoding.BigEndianUnicode.GetString(block, 2, 20).TrimEnd('\0');
            return raw == "" ? "Unnamed Mii" : raw;
        }
        catch { return "Unnamed Mii"; }
    }

    static long MiiId(byte[] block)
    {
        if (block.Length < 0x1C) return 0;
        return ((long)block[0x18] << 24) | ((long)block[0x19] << 16) | ((long)block[0x1A] << 8) | block[0x1B];
    }

    static bool Any(byte[] b, int off, int len)
    {
        for (int i = off; i < off + len && i < b.Length; i++)
            if (b[i] != 0) return true;
        return false;
    }

    static byte[] Slice(byte[] raw, int off, int len)
    {
        if (off < 0 || off >= raw.Length) return new byte[0];
        int n = Math.Min(len, raw.Length - off);
        var b = new byte[n];
        Array.Copy(raw, off, b, 0, n);
        return b;
    }

    static void CreateEmptyDb(string dbPath)
    {
        // Same Wii database layout WheelWizard uses: 100 74-byte blocks from 0x04,
        // RNOD/RNHD headers, CRC-16/CCITT at 0x1F1DE.
        var raw = new byte[779968];
        Encoding.ASCII.GetBytes("RNOD").CopyTo(raw, 0);
        raw[0x1CE0 + 0x0C] = 0x80;
        Encoding.ASCII.GetBytes("RNHD").CopyTo(raw, 0x1D00);
        raw[0x1D04] = raw[0x1D05] = raw[0x1D06] = raw[0x1D07] = 0xFF;
        ushort crc = Crc16(raw, CrcOffset);
        raw[CrcOffset] = (byte)(crc >> 8);
        raw[CrcOffset + 1] = (byte)(crc & 0xFF);
        Directory.CreateDirectory(Path.GetDirectoryName(dbPath));
        File.WriteAllBytes(dbPath, raw);
    }

    static JsonNode NullableInt(JsonNode n)
    {
        return n == null ? null : JsonValue.Create(ToInt(n));
    }

    static long ToInt(JsonNode n)
    {
        try { return n.GetValue<long>(); }
        catch
        {
            try { return (long)n.GetValue<double>(); } catch { return long.Parse(n.ToString()); }
        }
    }

    // ------------------------------------------------------------ for the Mii editor page
    public static JsonObject GetMiis()
    {
        lock (L)
        {
            string db = DbPath();
            var staged = ReadLibrary();
            var records = new List<JsonObject>();
            if (db != null && File.Exists(db))
            {
                try
                {
                    var raw = File.ReadAllBytes(db);
                    if (raw.Length >= CrcOffset + 2)
                    {
                        for (int slot = 0; slot < SlotCount; slot++)
                        {
                            int off = HeaderOffset + slot * BlockSize;
                            var block = Slice(raw, off, BlockSize);
                            if (block.Length != BlockSize || !Any(block, 0, block.Length)) continue;
                            records.Add(new JsonObject
                            {
                                ["slot"] = slot,
                                ["source_slot"] = slot,
                                ["stage_id"] = null,
                                ["name"] = MiiName(block),
                                ["data"] = Convert.ToBase64String(block),
                                ["origin"] = "Dolphin",
                            });
                        }
                    }
                }
                catch (IOException e)
                {
                    return new JsonObject { ["ok"] = false, ["error"] = "Couldn't read Dolphin's Mii database: " + e.Message };
                }
            }

            // Overlay staged edits onto the slot they came from, and append new Miis, so the
            // editor works before anything touches Dolphin.
            foreach (var node in staged)
            {
                try
                {
                    if (node is not JsonObject item) continue;
                    byte[] block = Convert.FromBase64String(item.Str("data"));
                    if (block.Length != BlockSize) continue;
                    JsonNode src = item["source_slot"];
                    var rec = new JsonObject
                    {
                        ["slot"] = src != null ? ToInt(src) : -1,
                        ["source_slot"] = NullableInt(src),
                        ["stage_id"] = item["stage_id"] == null ? null : JsonValue.Create(item.Str("stage_id")),
                        ["name"] = MiiName(block),
                        ["data"] = Convert.ToBase64String(block),
                        ["origin"] = "Nitro library",
                    };
                    int existing = -1;
                    if (src != null)
                    {
                        long s = ToInt(src);
                        for (int i = 0; i < records.Count; i++)
                            if (ToInt(records[i]["slot"]) == s) { existing = i; break; }
                    }
                    if (existing >= 0) records[existing] = rec;
                    else records.Add(rec);
                }
                catch { }
            }

            var arr = new JsonArray();
            foreach (var r in records) arr.Add(r);
            return new JsonObject
            {
                ["ok"] = true,
                ["path"] = db ?? "",
                ["miis"] = arr,
                ["message"] = "Miis are saved in Nitro and sync to Dolphin when Play is pressed.",
            };
        }
    }

    public static JsonObject SaveMii(long slot, string blockB64, string stageId)
    {
        lock (L)
        {
            try
            {
                byte[] block = Convert.FromBase64String(blockB64 ?? "");
                if (block.Length != BlockSize) throw new InvalidDataException("Invalid Wii Mii record. Expected 74 bytes.");
                var items = ReadLibrary();

                JsonObject existing = null;
                if (!string.IsNullOrEmpty(stageId))
                    foreach (var n in items)
                        if (n is JsonObject o && o.Str("stage_id") == stageId) { existing = o; break; }

                long? sourceSlot = null;
                if (existing != null)
                {
                    if (existing["source_slot"] != null) sourceSlot = ToInt(existing["source_slot"]);
                }
                else if (slot >= 0) sourceSlot = slot;

                long? sourceMiiId = null;
                if (existing != null && existing["source_mii_id"] != null) sourceMiiId = ToInt(existing["source_mii_id"]);

                // Remember the identity of the original slot, so sync won't overwrite a different
                // Mii if the Dolphin database changes before Play.
                string db = DbPath();
                if (sourceSlot.HasValue && !sourceMiiId.HasValue && db != null && File.Exists(db))
                {
                    try
                    {
                        var rawDb = File.ReadAllBytes(db);
                        var old = Slice(rawDb, HeaderOffset + (int)sourceSlot.Value * BlockSize, BlockSize);
                        if (old.Length == BlockSize && Any(old, 0, old.Length)) sourceMiiId = MiiId(old);
                    }
                    catch (IOException) { }
                }

                string newStage = existing != null ? existing.Str("stage_id") : Guid.NewGuid().ToString("N");
                var item = new JsonObject
                {
                    ["stage_id"] = newStage,
                    ["source_slot"] = sourceSlot.HasValue ? JsonValue.Create(sourceSlot.Value) : null,
                    ["source_mii_id"] = sourceMiiId.HasValue ? JsonValue.Create(sourceMiiId.Value) : null,
                    ["name"] = MiiName(block),
                    ["data"] = Convert.ToBase64String(block),
                };
                if (existing != null)
                {
                    int idx = items.IndexOf(existing);
                    items[idx] = item;
                }
                else items.Add(item);
                WriteLibrary(items);
                return new JsonObject
                {
                    ["ok"] = true,
                    ["slot"] = sourceSlot ?? -1,
                    ["stage_id"] = newStage,
                    ["name"] = MiiName(block),
                    ["message"] = "Saved to Nitro. It will be written to Dolphin when you press Play.",
                };
            }
            catch (Exception e)
            {
                return new JsonObject { ["ok"] = false, ["error"] = e.Message };
            }
        }
    }

    // ------------------------------------------------------------ sync into Dolphin
    public static MiiSyncResult SyncToDolphin()
    {
        lock (L)
        {
            var items = ReadLibrary();
            if (items.Count == 0) return new MiiSyncResult { Ok = true, Synced = 0 };
            string db = DbPath();
            if (db == null)
                return new MiiSyncResult { Ok = false, Error = "Choose your Dolphin executable in Settings before syncing Miis." };
            try
            {
                if (!File.Exists(db)) CreateEmptyDb(db);
                var raw = File.ReadAllBytes(db);
                if (raw.Length < CrcOffset + 2)
                    throw new InvalidDataException("RFL_DB.dat is too small to be a valid Wii Mii database: " + db);
                int stored = (raw[CrcOffset] << 8) | raw[CrcOffset + 1];
                int calc = Crc16(raw, CrcOffset);
                if (stored != calc)
                    throw new InvalidDataException(
                        $"Dolphin's Mii database CRC is invalid (stored {stored:X4}, expected {calc:X4}). No changes were written.");

                int changed = 0;
                foreach (var node in items)
                {
                    if (node is not JsonObject item) continue;
                    byte[] block = Convert.FromBase64String(item.Str("data"));
                    if (block.Length != BlockSize) continue;
                    long blockId = MiiId(block);
                    long? sourceSlot = item["source_slot"] != null ? ToInt(item["source_slot"]) : (long?)null;
                    long? sourceId = item["source_mii_id"] != null ? ToInt(item["source_mii_id"]) : (long?)null;
                    int chosen = -1;

                    if (sourceSlot.HasValue && sourceSlot.Value >= 0 && sourceSlot.Value < SlotCount)
                    {
                        int slot = (int)sourceSlot.Value;
                        var current = Slice(raw, HeaderOffset + slot * BlockSize, BlockSize);
                        long currentId = MiiId(current);
                        bool empty = !Any(current, 0, current.Length);
                        bool same = current.SequenceEqual(block);
                        if (empty || same || !sourceId.HasValue || currentId == sourceId.Value
                            || (blockId != 0 && currentId == blockId))
                            chosen = slot;
                    }

                    // If the original slot moved, find the same Mii by its client ID.
                    if (chosen < 0 && sourceId.HasValue && sourceId.Value != 0)
                    {
                        for (int slot = 0; slot < SlotCount; slot++)
                        {
                            var current = Slice(raw, HeaderOffset + slot * BlockSize, BlockSize);
                            if (Any(current, 0, current.Length) && MiiId(current) == sourceId.Value) { chosen = slot; break; }
                        }
                    }

                    // New Mii, or the original slot now holds a different Mii: update an exact-ID
                    // match, otherwise take the first empty slot.
                    if (chosen < 0 && blockId != 0)
                    {
                        for (int slot = 0; slot < SlotCount; slot++)
                        {
                            var current = Slice(raw, HeaderOffset + slot * BlockSize, BlockSize);
                            if (Any(current, 0, current.Length) && MiiId(current) == blockId) { chosen = slot; break; }
                        }
                    }
                    if (chosen < 0)
                    {
                        for (int slot = 0; slot < SlotCount; slot++)
                        {
                            var current = Slice(raw, HeaderOffset + slot * BlockSize, BlockSize);
                            if (!Any(current, 0, current.Length)) { chosen = slot; break; }
                        }
                    }
                    if (chosen < 0)
                        throw new InvalidOperationException("Dolphin's Mii database is full (100 slots). No further Miis can be added.");

                    Array.Copy(block, 0, raw, HeaderOffset + chosen * BlockSize, BlockSize);
                    item["source_slot"] = chosen;
                    long keep = blockId != 0 ? blockId : (sourceId ?? 0);
                    item["source_mii_id"] = keep != 0 ? JsonValue.Create(keep) : null;
                    item["name"] = MiiName(block);
                    changed++;
                }

                ushort newCrc = Crc16(raw, CrcOffset);
                raw[CrcOffset] = (byte)(newCrc >> 8);
                raw[CrcOffset + 1] = (byte)(newCrc & 0xFF);
                string backup = db + ".nitro-backup";
                if (!File.Exists(backup)) File.Copy(db, backup);
                string tmp = db + ".nitro.tmp";
                File.WriteAllBytes(tmp, raw);
                File.Move(tmp, db, true);
                WriteLibrary(items);
                return new MiiSyncResult { Ok = true, Synced = changed };
            }
            catch (Exception e)
            {
                return new MiiSyncResult { Ok = false, Error = e.Message };
            }
        }
    }

    // ------------------------------------------------------------ bundled files for the editor
    public static JsonObject FflResource()
    {
        try
        {
            string cfgPath = Cfg.Load().Str("ffl_resource_path");
            var candidates = new List<string>();
            if (cfgPath != "") candidates.Add(cfgPath);
            candidates.Add(Path.Combine(Paths.AppData, "FFLResHigh.dat"));
            candidates.Add(Path.Combine(Paths.AppData, "AFLResHigh_2_3.dat"));
            string exeDir = Path.GetDirectoryName(Environment.ProcessPath ?? "") ?? "";
            if (exeDir != "")
            {
                candidates.Add(Path.Combine(exeDir, "FFLResHigh.dat"));
                candidates.Add(Path.Combine(exeDir, "AFLResHigh_2_3.dat"));
            }
            byte[] data = null;
            foreach (var c in candidates)
            {
                try
                {
                    if (File.Exists(c) && new FileInfo(c).Length > 1024 * 1024) { data = File.ReadAllBytes(c); break; }
                }
                catch { }
            }
            if (data == null) data = Res.Bytes("res/RFL_Res.dat");
            if (data == null || data.Length < 1024 * 1024)
                return new JsonObject { ["ok"] = false, ["error"] = "No FFL resource found, including the one bundled in this build." };
            return new JsonObject
            {
                ["ok"] = true,
                ["size"] = data.Length,
                ["data"] = Convert.ToBase64String(data),
            };
        }
        catch (Exception e)
        {
            return new JsonObject { ["ok"] = false, ["error"] = e.Message };
        }
    }

    public static JsonObject FflResourceState()
    {
        var r = FflResource();
        r.Remove("data");
        return r;
    }

    public static JsonObject DefaultMii()
    {
        var data = Res.Bytes("res/starter.mii");
        if (data == null) return new JsonObject { ["ok"] = false, ["error"] = "starter.mii is not bundled." };
        return new JsonObject { ["ok"] = true, ["size"] = data.Length, ["data"] = Convert.ToBase64String(data) };
    }
}
