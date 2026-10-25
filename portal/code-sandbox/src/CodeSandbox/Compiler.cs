using System.Collections.Immutable;
using System.Globalization;
using System.Text;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.Emit;
using Microsoft.CodeAnalysis.Text;

namespace CodeSandbox;

/// <summary>Result of a compilation with counters for the log.</summary>
internal sealed record CompileOutcome(CompileResponse Response, int Errors, int Warnings, int Findings);

/// <summary>
/// Compiles project files with Roslyn in memory (portal-api.md §7.5.12): no MSBuild, no project
/// files, no analyzers or source generators; the compiler reads no files except the references
/// given here, and nothing compiled is loaded for execution.
/// </summary>
internal sealed class Compiler(ReferencePack referencePack, SdkState sdk, SandboxOptions options)
{
    /// <summary>Message culture of the diagnostics.</summary>
    public static readonly CultureInfo MessageCulture = CultureInfo.GetCultureInfo("ru");

    private const int MessageMaxChars = 1000;

    /// <summary>Implicit usings of the .NET 8 class library template.</summary>
    private const string GlobalUsings = """
        global using global::System;
        global using global::System.Collections.Generic;
        global using global::System.IO;
        global using global::System.Linq;
        global using global::System.Net.Http;
        global using global::System.Threading;
        global using global::System.Threading.Tasks;

        """;

    /// <summary>Symbols of a Release build for net8.0 as in the .NET 8 SDK.</summary>
    private static readonly string[] PreprocessorSymbols =
    [
        "TRACE", "RELEASE", "NET", "NET8_0", "NETCOREAPP",
        "NET5_0_OR_GREATER", "NET6_0_OR_GREATER", "NET7_0_OR_GREATER", "NET8_0_OR_GREATER",
        "NETCOREAPP1_0_OR_GREATER", "NETCOREAPP1_1_OR_GREATER", "NETCOREAPP2_0_OR_GREATER",
        "NETCOREAPP2_1_OR_GREATER", "NETCOREAPP2_2_OR_GREATER", "NETCOREAPP3_0_OR_GREATER",
        "NETCOREAPP3_1_OR_GREATER",
    ];

    /// <summary>Parse options: C# 12, regular files (no <c>#r</c> or <c>#load</c>), documentation comments not processed.</summary>
    public static readonly CSharpParseOptions ParseOptions = new(
        languageVersion: LanguageVersion.CSharp12,
        documentationMode: DocumentationMode.None,
        kind: SourceCodeKind.Regular,
        preprocessorSymbols: PreprocessorSymbols);

    /// <summary>
    /// Library, Release, deterministic, nullable on, no unsafe, warning level 8 and NoWarn 1701;1702
    /// as in the .NET 8 SDK; resolvers and the strong name provider are explicitly absent.
    /// </summary>
    public static CSharpCompilationOptions CreateCompilationOptions() =>
        new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary)
            .WithOptimizationLevel(OptimizationLevel.Release)
            .WithDeterministic(true)
            .WithNullableContextOptions(NullableContextOptions.Enable)
            .WithAllowUnsafe(false)
            .WithWarningLevel(8)
            .WithSpecificDiagnosticOptions(new Dictionary<string, ReportDiagnostic>
            {
                ["CS1701"] = ReportDiagnostic.Suppress,
                ["CS1702"] = ReportDiagnostic.Suppress,
            })
            .WithXmlReferenceResolver(null)
            .WithSourceReferenceResolver(null)
            .WithMetadataReferenceResolver(null)
            .WithStrongNameProvider(null);

    /// <summary>Compiles the checked request; cancellation is the time limit or the closed connection.</summary>
    public CompileOutcome Compile(ValidRequest request, CancellationToken cancellationToken)
    {
        var version = sdk.Version ?? throw new InvalidOperationException("SDK is not loaded.");
        var (packageReferences, bundle, onServer) = ResolvePackages(request.Packages);

        var trees = request.Files
            .Select(file => CSharpSyntaxTree.ParseText(
                SourceText.From(file.Content, Encoding.UTF8), ParseOptions, file.Path, cancellationToken))
            .ToList();
        var globalUsings = CSharpSyntaxTree.ParseText(GlobalUsings, ParseOptions, string.Empty, cancellationToken: cancellationToken);

        var compilation = CSharpCompilation.Create(
            request.AssemblyName,
            trees.Append(globalUsings),
            referencePack.References.Concat(sdk.References).Concat(packageReferences),
            CreateCompilationOptions());

        using var peStream = new MemoryStream();
        var emit = compilation.Emit(peStream, options: new EmitOptions(), cancellationToken: cancellationToken);

        var reported = emit.Diagnostics
            .Where(diagnostic => !diagnostic.IsSuppressed && diagnostic.Severity is DiagnosticSeverity.Error or DiagnosticSeverity.Warning)
            .Distinct()
            .Select(diagnostic => ToDto(diagnostic, globalUsings))
            .OrderBy(diagnostic => diagnostic.Severity == "error" ? 0 : 1)
            .ThenBy(diagnostic => diagnostic.File is null ? 0 : 1)
            .ThenBy(diagnostic => diagnostic.File, StringComparer.OrdinalIgnoreCase)
            .ThenBy(diagnostic => diagnostic.Line)
            .ThenBy(diagnostic => diagnostic.Column)
            .ThenBy(diagnostic => diagnostic.Code, StringComparer.Ordinal)
            .ToList();
        var errors = reported.Count(diagnostic => diagnostic.Severity == "error");
        var warnings = reported.Count - errors;
        var shown = reported.Take(options.DiagnosticsMax).ToList();
        var truncated = reported.Count > shown.Count;

        if (!emit.Success || errors > 0)
        {
            return new CompileOutcome(
                new CompileResponse("failed", version, shown, truncated, null, null, [], []), errors, warnings, 0);
        }

        var rules = new GpToolRules(options.GpTool).Check(compilation, trees, cancellationToken);
        return new CompileOutcome(
            new CompileResponse("succeeded", version, shown, truncated, rules, peStream.ToArray(), bundle, onServer),
            errors,
            warnings,
            rules.Findings.Count);
    }

    /// <summary>Answer of a compilation stopped by the time limit.</summary>
    public static CompileResponse Timeout(string sdkVersion) => new("timeout", sdkVersion, [], false, null, null, [], []);

    private (List<MetadataReference> References, List<BundleFile> Bundle, List<string> OnServer) ResolvePackages(IReadOnlyList<ValidPackage> requested)
    {
        var references = new List<MetadataReference>();
        var bundle = new List<BundleFile>();
        var onServer = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var package in requested)
        {
            foreach (var library in PackageStore.GetLibraries(package.NupkgPath))
            {
                if (!seen.Add(library.FileName))
                {
                    continue;
                }

                // A server library has priority: the SDK copy is already referenced (decision 25).
                if (sdk.ServerCopies.Contains(library.FileName))
                {
                    onServer.Add(library.FileName);
                    continue;
                }

                references.Add(MetadataReference.CreateFromImage(library.Image, filePath: library.FileName));
                if (sdk.OnServer.Contains(library.FileName))
                {
                    onServer.Add(library.FileName);
                }
                else
                {
                    bundle.Add(new BundleFile(library.FileName, library.Image));
                }
            }
        }

        return (references, bundle, onServer);
    }

    private static DiagnosticDto ToDto(Diagnostic diagnostic, SyntaxTree globalUsings)
    {
        var message = diagnostic.GetMessage(MessageCulture);
        if (message.Length > MessageMaxChars)
        {
            message = message[..MessageMaxChars];
        }

        var severity = diagnostic.Severity == DiagnosticSeverity.Error ? "error" : "warning";
        var location = diagnostic.Location;
        if (!location.IsInSource || location.SourceTree == globalUsings)
        {
            return new DiagnosticDto(severity, diagnostic.Id, null, null, null, null, null, message);
        }

        // Unmapped span: "#line" directives do not change the reported file or line.
        var span = location.GetLineSpan();
        return new DiagnosticDto(
            severity,
            diagnostic.Id,
            location.SourceTree!.FilePath,
            span.StartLinePosition.Line + 1,
            span.StartLinePosition.Character + 1,
            span.EndLinePosition.Line + 1,
            span.EndLinePosition.Character + 1,
            message);
    }
}
