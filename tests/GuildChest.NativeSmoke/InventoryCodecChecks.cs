using System;
using System.Reflection;

namespace GuildChest.NativeSmoke;

public sealed partial class NativeSmokeHarness
{
    private void VerifyInventoryFormats()
    {
        var inventory = CreateFixtureInventory();
        var current = Encode(inventory);
        int version = new ZPackage(current).ReadInt();
        Check(version == 109 || version == 110, "Native inventory writes an inspected format");
        ValidateItems(Decode(current));

        // Both inspected formats have the same record layout. Valheim 1.0.20
        // starts reading the existing trailing cheat flag only in format 110.
        var legacy = new ZPackage();
        legacy.Write(109);
        legacy.Write((ushort)inventory.NrOfItems());
        foreach (var item in inventory.GetAllItems()) item.Save(legacy);
        var restored = Decode(legacy.GetArray());
        ValidateItems(restored);
        ValidateItems(Decode(Encode(restored)));

        if (version == 110)
        {
            inventory.GetItemAt(1, 0).m_cheated = true;
            Check(Decode(Encode(inventory)).GetItemAt(1, 0).m_cheated, "Format 110 retains the native cheat flag");
        }

        foreach (int unsupported in new[] { 108, 111 })
        {
            var package = new ZPackage();
            package.Write(unsupported);
            package.Write((ushort)0);
            bool rejected = false;
            try { Decode(package.GetArray()); }
            catch (TargetInvocationException exception)
            {
                rejected = exception.InnerException is InvalidOperationException &&
                    exception.InnerException.Message.Contains($"Unsupported inventory format {unsupported}");
            }
            Check(rejected, $"Uninspected inventory format {unsupported} is rejected");
        }
        Logger.LogInfo("NATIVE_SMOKE_INVENTORY_FORMATS_PASSED");
    }
}
