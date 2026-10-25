using System.IO.Compression;

namespace CodeSandbox;

/// <summary>Library of a package: file name and image.</summary>
internal sealed record PackageLibrary(string FileName, byte[] Image);

/// <summary>
/// Reads package libraries straight from <c>.nupkg</c> into memory: only the DLL of
/// <c>lib/&lt;tfm&gt;/</c> for the first suitable target; <c>analyzers/</c>, <c>build/</c> and
/// <c>tools/</c> are never read (portal-api.md §7.5.12). Nothing is unpacked to disk.
/// </summary>
internal static class PackageStore
{
    private static readonly string[] TargetFrameworks =
        ["net8.0", "net7.0", "net6.0", "net5.0", "netcoreapp3.1", "netstandard2.1", "netstandard2.0"];

    /// <summary>Libraries of a package, by file name.</summary>
    public static IReadOnlyList<PackageLibrary> GetLibraries(string nupkgPath)
    {
        using var archive = ZipFile.OpenRead(nupkgPath);
        var byFolder = archive.Entries
            .Select(entry => (Entry: entry, Parts: entry.FullName.Replace('\\', '/').Split('/')))
            .Where(item => item.Parts.Length == 3
                && item.Parts[0].Equals("lib", StringComparison.OrdinalIgnoreCase)
                && item.Parts[2].EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
            .ToLookup(item => item.Parts[1], StringComparer.OrdinalIgnoreCase);
        var folder = TargetFrameworks.FirstOrDefault(tfm => byFolder.Contains(tfm));
        if (folder is null)
        {
            return [];
        }

        return byFolder[folder]
            .OrderBy(item => item.Parts[2], StringComparer.Ordinal)
            .Select(item =>
            {
                using var stream = item.Entry.Open();
                using var buffer = new MemoryStream();
                stream.CopyTo(buffer);
                return new PackageLibrary(item.Parts[2], buffer.ToArray());
            })
            .ToArray();
    }
}
