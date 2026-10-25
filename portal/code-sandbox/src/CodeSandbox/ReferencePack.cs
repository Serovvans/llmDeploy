using System.Collections.Immutable;
using Microsoft.CodeAnalysis;

namespace CodeSandbox;

/// <summary>.NET 8 reference assemblies read into memory once (metadata only, never executed).</summary>
internal sealed class ReferencePack
{
    private ReferencePack(ImmutableArray<MetadataReference> references, ImmutableHashSet<string> assemblyNames)
    {
        References = references;
        AssemblyNames = assemblyNames;
    }

    /// <summary>References for a compilation.</summary>
    public ImmutableArray<MetadataReference> References { get; }

    /// <summary>Simple assembly names of the pack, case-insensitive (file name equals assembly name in the pack).</summary>
    public ImmutableHashSet<string> AssemblyNames { get; }

    /// <summary>Reads every <c>*.dll</c> of the folder; a missing folder is a packaging error.</summary>
    public static ReferencePack Load(string directory)
    {
        var files = Directory.GetFiles(directory, "*.dll").Order(StringComparer.Ordinal).ToArray();
        if (!files.Any(file => Path.GetFileName(file) == "System.Runtime.dll"))
        {
            throw new InvalidOperationException("Reference pack has no System.Runtime.dll.");
        }

        var references = files
            .Select(file => (MetadataReference)MetadataReference.CreateFromImage(File.ReadAllBytes(file), filePath: Path.GetFileName(file)))
            .ToImmutableArray();
        var names = files.Select(Path.GetFileNameWithoutExtension).OfType<string>()
            .ToImmutableHashSet(StringComparer.OrdinalIgnoreCase);
        return new ReferencePack(references, names);
    }
}
