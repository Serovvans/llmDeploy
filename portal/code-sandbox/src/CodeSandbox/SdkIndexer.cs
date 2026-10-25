using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using System.Security.Cryptography;
using System.Text.Json;

namespace CodeSandbox;

/// <summary>
/// <c>code-sandbox index --sdk &lt;dir&gt; --out &lt;dir&gt;</c> (portal-api.md §7.5.14): checks the SDK
/// folder, writes <c>manifest.json</c> and <c>reference.json</c>. Reads metadata only.
/// </summary>
internal static class SdkIndexer
{
    /// <summary>Required CoGIS libraries.</summary>
    public static readonly string[] RequiredFiles = ["eLiteGIS.Server.dll", "Atemiko.eLiteMap.dll"];

    /// <summary>List of the server folder <c>/usr/elitegis/plugins/</c>.</summary>
    public const string PluginsFile = "plugins.txt";

    private const string VersionSource = "eLiteGIS.Server.dll";

    /// <summary>Builds the index; returns the exit code: 0 — written, 1 — error, nothing written.</summary>
    public static int Run(string sdkDir, string outDir, string referenceDir, TextWriter output, TextWriter error)
    {
        if (!Directory.Exists(sdkDir))
        {
            error.WriteLine($"error: SDK folder not found: {sdkDir}");
            return 1;
        }

        var entries = Directory.GetFiles(sdkDir).Select(path => Path.GetFileName(path)).Order(StringComparer.Ordinal).ToList();
        var pluginsName = entries.FirstOrDefault(name => name.Equals(PluginsFile, StringComparison.OrdinalIgnoreCase));
        var failed = false;
        foreach (var required in RequiredFiles.Where(required => !entries.Contains(required, StringComparer.OrdinalIgnoreCase)))
        {
            error.WriteLine($"error: required file missing: {required}");
            failed = true;
        }

        if (pluginsName is null)
        {
            error.WriteLine($"error: required file missing: {PluginsFile}");
            failed = true;
        }

        if (failed)
        {
            return 1;
        }

        var referencePack = ReferencePack.Load(referenceDir);
        var files = new List<SdkFile>();
        var images = new List<(SdkFile File, byte[] Image)>();
        Identity? versionSource = null;
        foreach (var name in entries.Where(name => name.EndsWith(".dll", StringComparison.OrdinalIgnoreCase)))
        {
            var image = File.ReadAllBytes(Path.Combine(sdkDir, name));
            var identity = ReadIdentity(image);
            var required = RequiredFiles.Contains(name, StringComparer.OrdinalIgnoreCase);
            if (identity is null)
            {
                if (required)
                {
                    error.WriteLine($"error: not a .NET assembly: {name}");
                    return 1;
                }

                error.WriteLine($"warning: not a .NET assembly, skipped: {name}");
                continue;
            }

            var kind = name.StartsWith("eLiteGIS.", StringComparison.OrdinalIgnoreCase) || name.StartsWith("Atemiko.", StringComparison.OrdinalIgnoreCase)
                ? SdkKinds.Cogis
                : referencePack.AssemblyNames.Contains(identity.Name) ? SdkKinds.Framework : SdkKinds.Server;
            var file = new SdkFile(name, kind, identity.Version, identity.FileVersion, Convert.ToHexString(SHA256.HashData(image)).ToLowerInvariant());
            files.Add(file);
            images.Add((file, image));
            if (name.Equals(VersionSource, StringComparison.OrdinalIgnoreCase))
            {
                versionSource = identity;
            }
        }

        var sdkVersion = versionSource!.InformationalVersion ?? versionSource.FileVersion ?? versionSource.Version;
        var plugins = ReadPlugins(Path.Combine(sdkDir, pluginsName!));

        var manifest = new SdkManifest(SdkManifest.CurrentFormat, sdkVersion, files, plugins);
        var reference = ApiReference.Build(sdkVersion, images, referencePack);

        Directory.CreateDirectory(outDir);
        WriteAtomically(Path.Combine(outDir, ApiReference.FileName), reference);
        WriteAtomically(Path.Combine(outDir, SdkManifest.FileName), manifest);

        output.WriteLine($"sdk_version: {sdkVersion}");
        output.WriteLine($"{"file",-60} {"kind",-9} {"version",-20} sha256");
        foreach (var file in files)
        {
            output.WriteLine($"{file.FileName,-60} {file.Kind,-9} {file.FileVersion ?? file.AssemblyVersion,-20} {file.Sha256[..12]}");
        }

        output.WriteLine($"plugins.txt: {plugins.Count} names; reference: {reference.Types.Count} types");
        return 0;
    }

    /// <summary>Names of <c>plugins.txt</c>: trimmed, empty lines and names not ending with <c>.dll</c> skipped.</summary>
    public static List<string> ReadPlugins(string path) =>
        File.ReadAllLines(path)
            .Select(line => line.Trim())
            .Where(line => line.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();

    private sealed record Identity(string Name, string Version, string? FileVersion, string? InformationalVersion);

    /// <summary>Assembly name and versions from metadata; <c>null</c> — not a .NET assembly.</summary>
    private static Identity? ReadIdentity(byte[] image)
    {
        try
        {
            using var pe = new PEReader(new MemoryStream(image));
            if (!pe.HasMetadata)
            {
                return null;
            }

            var reader = pe.GetMetadataReader();
            if (!reader.IsAssembly)
            {
                return null;
            }

            var assembly = reader.GetAssemblyDefinition();
            string? fileVersion = null;
            string? informational = null;
            foreach (var handle in assembly.GetCustomAttributes())
            {
                var attribute = reader.GetCustomAttribute(handle);
                switch (AttributeTypeName(reader, attribute))
                {
                    case "System.Reflection.AssemblyFileVersionAttribute":
                        fileVersion = SingleStringArgument(reader, attribute);
                        break;
                    case "System.Reflection.AssemblyInformationalVersionAttribute":
                        informational = SingleStringArgument(reader, attribute);
                        break;
                }
            }

            return new Identity(reader.GetString(assembly.Name), assembly.Version.ToString(), fileVersion, informational);
        }
        catch (BadImageFormatException)
        {
            return null;
        }
    }

    private static string? AttributeTypeName(MetadataReader reader, CustomAttribute attribute)
    {
        EntityHandle type;
        if (attribute.Constructor.Kind == HandleKind.MemberReference)
        {
            type = reader.GetMemberReference((MemberReferenceHandle)attribute.Constructor).Parent;
        }
        else if (attribute.Constructor.Kind == HandleKind.MethodDefinition)
        {
            type = reader.GetMethodDefinition((MethodDefinitionHandle)attribute.Constructor).GetDeclaringType();
        }
        else
        {
            return null;
        }

        return type.Kind switch
        {
            HandleKind.TypeReference => Join(reader.GetTypeReference((TypeReferenceHandle)type).Namespace, reader.GetTypeReference((TypeReferenceHandle)type).Name),
            HandleKind.TypeDefinition => Join(reader.GetTypeDefinition((TypeDefinitionHandle)type).Namespace, reader.GetTypeDefinition((TypeDefinitionHandle)type).Name),
            _ => null,
        };

        string Join(StringHandle ns, StringHandle name) => $"{reader.GetString(ns)}.{reader.GetString(name)}";
    }

    private static string? SingleStringArgument(MetadataReader reader, CustomAttribute attribute)
    {
        var blob = reader.GetBlobReader(attribute.Value);
        if (blob.Length < 2 || blob.ReadUInt16() != 1)
        {
            return null;
        }

        var value = blob.ReadSerializedString();
        return string.IsNullOrWhiteSpace(value) ? null : value;
    }

    private static void WriteAtomically<T>(string path, T value)
    {
        var temporary = path + ".tmp";
        File.WriteAllBytes(temporary, JsonSerializer.SerializeToUtf8Bytes(value, Json.Options));
        File.Move(temporary, path, overwrite: true);
    }
}
