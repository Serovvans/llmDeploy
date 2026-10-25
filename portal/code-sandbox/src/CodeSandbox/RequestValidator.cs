using System.Text;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeSandbox;

/// <summary>Checked compile request: names and paths are safe, packages exist in the offline set.</summary>
internal sealed record ValidRequest(string AssemblyName, IReadOnlyList<ValidFile> Files, IReadOnlyList<ValidPackage> Packages, int SourceBytes);

/// <summary>Checked project file.</summary>
internal sealed record ValidFile(string Path, string Content);

/// <summary>Package of the offline set and the path of its <c>.nupkg</c>.</summary>
internal sealed record ValidPackage(string Id, string Version, string NupkgPath);

/// <summary>Refusal of a request before compilation: <c>invalid_request</c> or <c>unknown_package</c>.</summary>
internal sealed record Rejection(string Error, string Message);

/// <summary>Input checks of portal-api.md §7.5.3 and §7.5.12; messages never echo the input.</summary>
internal static partial class RequestValidator
{
    private const int PathMaxChars = 200;
    private const int PathMaxParts = 8;
    private const string Extension = ".cs";

    private static readonly HashSet<string> WindowsReserved = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    };

    [GeneratedRegex("^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    private static partial Regex NamePattern();

    [GeneratedRegex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")]
    private static partial Regex PathPartPattern();

    [GeneratedRegex("^[A-Za-z]:")]
    private static partial Regex DrivePattern();

    [GeneratedRegex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")]
    private static partial Regex PackageIdPattern();

    [GeneratedRegex("^[0-9][A-Za-z0-9.+-]{0,63}$")]
    private static partial Regex PackageVersionPattern();

    /// <summary>Checks the request; returns the checked request or the refusal.</summary>
    public static (ValidRequest? Request, Rejection? Rejection) Validate(CompileRequest? request, SandboxOptions options)
    {
        if (request is null || request.AssemblyName is null || request.Files is null || request.Packages is null)
        {
            return Reject("Нет обязательного поля: assembly_name, files или packages.");
        }

        if (!IsValidProjectName(request.AssemblyName))
        {
            return Reject("Недопустимое имя сборки.");
        }

        if (request.Files.Count > options.MaxFiles)
        {
            return Reject($"Файлов больше {options.MaxFiles}.");
        }

        var files = new List<ValidFile>(request.Files.Count);
        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var totalBytes = 0;
        for (var i = 0; i < request.Files.Count; i++)
        {
            var number = i + 1;
            var file = request.Files[i];
            if (file?.Path is null || file.Content is null)
            {
                return Reject($"Файл {number}: нет пути или содержимого.");
            }

            var pathError = CheckPath(file.Path);
            if (pathError is not null)
            {
                return Reject($"Файл {number}: {pathError}");
            }

            if (!paths.Add(file.Path))
            {
                return Reject($"Файл {number}: путь повторяется.");
            }

            if (file.Content.Contains('\0'))
            {
                return Reject($"Файл {number}: символ U+0000 в содержимом.");
            }

            var bytes = Encoding.UTF8.GetByteCount(file.Content);
            if (bytes > options.FileMaxBytes)
            {
                return Reject($"Файл {number}: больше {options.FileMaxBytes} байт.");
            }

            totalBytes += bytes;
            if (totalBytes > options.ProjectMaxBytes)
            {
                return Reject($"Файлы вместе больше {options.ProjectMaxBytes} байт.");
            }

            files.Add(new ValidFile(file.Path, file.Content));
        }

        foreach (var path in paths)
        {
            // A file path must not be the folder of another file ("a.cs" and "A.CS/B.cs").
            var slash = path.IndexOf('/');
            while (slash > 0)
            {
                if (paths.Contains(path[..slash]))
                {
                    return Reject("Путь файла совпадает с папкой другого файла.");
                }

                slash = path.IndexOf('/', slash + 1);
            }
        }

        if (request.Packages.Count > options.MaxPackages)
        {
            return Reject($"Пакетов больше {options.MaxPackages}.");
        }

        var packages = new List<ValidPackage>(request.Packages.Count);
        var ids = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (var i = 0; i < request.Packages.Count; i++)
        {
            var number = i + 1;
            var package = request.Packages[i];
            if (package?.Id is null || package.Version is null
                || !PackageIdPattern().IsMatch(package.Id) || !PackageVersionPattern().IsMatch(package.Version)
                || package.Id.Contains("..", StringComparison.Ordinal) || package.Version.Contains("..", StringComparison.Ordinal))
            {
                return Reject($"Пакет {number}: недопустимый id или версия.");
            }

            if (!ids.Add(package.Id))
            {
                return Reject($"Пакет {number}: id повторяется.");
            }

            var id = package.Id.ToLowerInvariant();
            var version = package.Version.ToLowerInvariant();
            var nupkg = Path.Combine(options.PackagesDir, id, version, $"{id}.{version}.nupkg");
            if (!File.Exists(nupkg))
            {
                return (null, new Rejection("unknown_package", $"Пакет {number}: нет в офлайн-наборе."));
            }

            packages.Add(new ValidPackage(package.Id, package.Version, nupkg));
        }

        return (new ValidRequest(request.AssemblyName, files, packages, totalBytes), null);
    }

    /// <summary>Project (assembly) name of §7.5.3: identifier, not a C# keyword, not a reserved Windows name.</summary>
    public static bool IsValidProjectName(string name) =>
        NamePattern().IsMatch(name)
        && SyntaxFacts.GetKeywordKind(name) == SyntaxKind.None
        && !WindowsReserved.Contains(name);

    /// <summary>Checks a file path of §7.5.3; returns the reason or <c>null</c>.</summary>
    public static string? CheckPath(string path)
    {
        if (path.Length is 0 or > PathMaxChars)
        {
            return "длина пути вне 1…200.";
        }

        if (path.StartsWith('/') || path.Contains('\\') || DrivePattern().IsMatch(path))
        {
            return "путь вне проекта.";
        }

        var parts = path.Split('/');
        if (parts.Any(part => part is "." or ".."))
        {
            return "путь вне проекта.";
        }

        if (parts.Length > PathMaxParts)
        {
            return "больше 8 частей пути.";
        }

        foreach (var part in parts)
        {
            if (!PathPartPattern().IsMatch(part) || part.EndsWith('.'))
            {
                return "недопустимые символы в пути.";
            }

            var dot = part.IndexOf('.');
            if (WindowsReserved.Contains(dot < 0 ? part : part[..dot]))
            {
                return "зарезервированное имя Windows в пути.";
            }
        }

        return path.EndsWith(Extension, StringComparison.Ordinal) ? null : "расширение не .cs.";
    }

    private static (ValidRequest?, Rejection?) Reject(string message) => (null, new Rejection("invalid_request", message));
}
