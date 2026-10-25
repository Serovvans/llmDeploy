namespace CodeSandbox;

/// <summary><c>manifest.json</c> of the SDK index (portal-api.md §7.5.14).</summary>
internal sealed record SdkManifest(int Format, string SdkVersion, IReadOnlyList<SdkFile> Files, IReadOnlyList<string> Plugins)
{
    /// <summary>Supported format.</summary>
    public const int CurrentFormat = 1;

    /// <summary>File name inside the index folder.</summary>
    public const string FileName = "manifest.json";
}

/// <summary>DLL of the SDK folder: kind <c>cogis</c>, <c>server</c> or <c>framework</c>; SHA-256 in lowercase hex.</summary>
internal sealed record SdkFile(string FileName, string Kind, string AssemblyVersion, string? FileVersion, string Sha256);

/// <summary>Kinds of SDK files.</summary>
internal static class SdkKinds
{
    /// <summary>Library of CoGIS itself (<c>eLiteGIS.*</c>, <c>Atemiko.*</c>).</summary>
    public const string Cogis = "cogis";

    /// <summary>Other library from the CoGIS server; has priority over packages.</summary>
    public const string Server = "server";

    /// <summary>Copy of a .NET 8 assembly: not referenced, the reference pack gives it.</summary>
    public const string Framework = "framework";
}
