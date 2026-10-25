using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CodeSandbox;

/// <summary>Body of <c>POST /compile</c> (portal-api.md §7.5.12).</summary>
internal sealed record CompileRequest(string? AssemblyName, List<SourceFile?>? Files, List<PackageRef?>? Packages);

/// <summary>Project file of a request.</summary>
internal sealed record SourceFile(string? Path, string? Content);

/// <summary>Package of the offline set with its exact version.</summary>
internal sealed record PackageRef(string? Id, string? Version);

/// <summary>Answer of <c>POST /compile</c>.</summary>
internal sealed record CompileResponse(
    string Status,
    string SdkVersion,
    IReadOnlyList<DiagnosticDto> Diagnostics,
    bool DiagnosticsTruncated,
    RulesDto? Rules,
    byte[]? Dll,
    IReadOnlyList<BundleFile> Bundle,
    IReadOnlyList<string> OnServer);

/// <summary>Compiler message; positions start at 1, <c>file</c> is the request path or <c>null</c>.</summary>
internal sealed record DiagnosticDto(
    string Severity,
    string Code,
    string? File,
    int? Line,
    int? Column,
    int? EndLine,
    int? EndColumn,
    string Message);

/// <summary>Result of the GPTool rules (portal-api.md §7.5.13).</summary>
internal sealed record RulesDto(string? Factory, IReadOnlyList<ToolDto> Tools, IReadOnlyList<FindingDto> Findings);

/// <summary>Tool of the project: full class name, Russian title and category from the attributes.</summary>
internal sealed record ToolDto(string Name, string? Title, string? Category);

/// <summary>Violation of a GPTool rule; position is <c>null</c> for a finding without a place.</summary>
internal sealed record FindingDto(string Rule, string? File, int? Line, int? Column, string Message);

/// <summary>Package library that is not on the CoGIS server and goes to the archive.</summary>
internal sealed record BundleFile(string FileName, byte[] Content);

/// <summary>Answer of <c>GET /health</c>.</summary>
internal sealed record HealthResponse(string? SdkVersion);

/// <summary>Refusal: <c>invalid_request</c>, <c>unknown_package</c>, <c>busy</c>, <c>sdk_missing</c>, <c>internal</c>.</summary>
internal sealed record ErrorResponse(
    string Error,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? Message = null);

/// <summary>JSON settings of the protocol and the index files.</summary>
internal static class Json
{
    /// <summary>snake_case, unknown fields rejected; non-ASCII and C# signatures are not escaped (the readers are JSON parsers, not HTML).</summary>
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        MaxDepth = 16,
    };
}
