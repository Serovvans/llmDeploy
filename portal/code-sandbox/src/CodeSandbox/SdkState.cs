using System.Collections.Immutable;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.CodeAnalysis;

namespace CodeSandbox;

/// <summary>
/// SDK checked at start: <c>manifest.json</c> of the index and the DLL of the SDK folder with the
/// same SHA-256. Without it every compilation answers <c>503 sdk_missing</c>.
/// </summary>
internal sealed class SdkState
{
    private SdkState(string? version, ImmutableArray<MetadataReference> references, ImmutableHashSet<string> serverCopies, ImmutableHashSet<string> onServer)
    {
        Version = version;
        References = references;
        ServerCopies = serverCopies;
        OnServer = onServer;
    }

    /// <summary><c>sdk_version</c> of the manifest; <c>null</c> — SDK is not loaded.</summary>
    public string? Version { get; }

    /// <summary>SDK DLL of kinds <c>cogis</c> and <c>server</c> for a compilation.</summary>
    public ImmutableArray<MetadataReference> References { get; }

    /// <summary>File names of kinds <c>cogis</c> and <c>server</c>: a package DLL with such a name is replaced by the server copy.</summary>
    public ImmutableHashSet<string> ServerCopies { get; }

    /// <summary>File names that are on the CoGIS server: every SDK DLL and <c>plugins.txt</c>.</summary>
    public ImmutableHashSet<string> OnServer { get; }

    /// <summary>Loads the SDK; any mismatch gives the "not loaded" state with a reason code.</summary>
    public static (SdkState State, string Reason) Load(string indexDir, string sdkDir)
    {
        var missing = new SdkState(null, [], [], []);
        var manifestPath = Path.Combine(indexDir, SdkManifest.FileName);
        if (!File.Exists(manifestPath))
        {
            return (missing, "no_manifest");
        }

        SdkManifest? manifest;
        try
        {
            manifest = JsonSerializer.Deserialize<SdkManifest>(File.ReadAllBytes(manifestPath), Json.Options);
        }
        catch (JsonException)
        {
            return (missing, "bad_manifest");
        }

        if (manifest is null || manifest.Format != SdkManifest.CurrentFormat || manifest.Files is null || manifest.Plugins is null)
        {
            return (missing, "bad_manifest");
        }

        var references = ImmutableArray.CreateBuilder<MetadataReference>();
        var serverCopies = ImmutableHashSet.CreateBuilder<string>(StringComparer.OrdinalIgnoreCase);
        var onServer = ImmutableHashSet.CreateBuilder<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var file in manifest.Files)
        {
            if (file.FileName != Path.GetFileName(file.FileName) || file.FileName is "." or "..")
            {
                return (missing, "bad_manifest");
            }

            var path = Path.Combine(sdkDir, file.FileName);
            if (!File.Exists(path))
            {
                return (missing, "file_missing");
            }

            // The bytes that were hashed are the bytes that are referenced.
            var image = File.ReadAllBytes(path);
            if (!string.Equals(Convert.ToHexString(SHA256.HashData(image)), file.Sha256, StringComparison.OrdinalIgnoreCase))
            {
                return (missing, "hash_mismatch");
            }

            onServer.Add(file.FileName);
            if (file.Kind is SdkKinds.Cogis or SdkKinds.Server)
            {
                serverCopies.Add(file.FileName);
                references.Add(MetadataReference.CreateFromImage(image, filePath: file.FileName));
            }
        }

        onServer.UnionWith(manifest.Plugins);
        return (new SdkState(manifest.SdkVersion, references.ToImmutable(), serverCopies.ToImmutable(), onServer.ToImmutable()), "ok");
    }
}
