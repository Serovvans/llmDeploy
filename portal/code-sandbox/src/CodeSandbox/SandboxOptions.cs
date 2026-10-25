using System.Text.Json.Serialization;

namespace CodeSandbox;

/// <summary>Settings of the sandbox, section <c>Sandbox</c> (environment: <c>Sandbox__MaxParallel</c> and so on).</summary>
internal sealed class SandboxOptions
{
    /// <summary>Configuration section name.</summary>
    public const string Section = "Sandbox";

    /// <summary>CoGIS SDK folder, read-only.</summary>
    public string SdkDir { get; set; } = "/sdk";

    /// <summary>Index folder with <c>manifest.json</c> and <c>reference.json</c>, read-only.</summary>
    public string IndexDir { get; set; } = "/index";

    /// <summary>Offline package set in the nuget.org layout, read-only.</summary>
    public string PackagesDir { get; set; } = "/packages";

    /// <summary>.NET 8 reference assemblies; <c>null</c> — <c>ref/</c> next to the binaries.</summary>
    public string? ReferenceDir { get; set; }

    /// <summary>Concurrent compilations; the next one gets <c>429 busy</c>.</summary>
    public int MaxParallel { get; set; } = 2;

    /// <summary>Compilation time limit, seconds; must be below <c>code.build.timeout_seconds</c> of the portal.</summary>
    public double TimeoutSeconds { get; set; } = 60;

    /// <summary>Files in a request; not below <c>code.max_files</c>.</summary>
    public int MaxFiles { get; set; } = 50;

    /// <summary>UTF-8 bytes of one file; not below <c>code.file_max_bytes</c>.</summary>
    public int FileMaxBytes { get; set; } = 16384;

    /// <summary>UTF-8 bytes of all files; not below <c>code.project_max_bytes</c>.</summary>
    public int ProjectMaxBytes { get; set; } = 262144;

    /// <summary>Packages in a request (with dependencies).</summary>
    public int MaxPackages { get; set; } = 50;

    /// <summary>Request body bytes.</summary>
    public long RequestMaxBytes { get; set; } = 4 * 1024 * 1024;

    /// <summary>Diagnostics in a response; the rest is cut with <c>diagnostics_truncated</c>.</summary>
    public int DiagnosticsMax { get; set; } = 200;

    /// <summary>Full names of the GPTool types the rules look for.</summary>
    public GpToolNames GpTool { get; set; } = new();

    /// <summary>Resolved folder of the reference assemblies.</summary>
    [JsonIgnore]
    public string ResolvedReferenceDir => ReferenceDir ?? Path.Combine(AppContext.BaseDirectory, "ref");
}

/// <summary>Full metadata names of the CoGIS types used by the GPTool rules (portal-api.md §7.5.13).</summary>
internal sealed class GpToolNames
{
    /// <summary>Base class of a tool.</summary>
    public string ToolBase { get; set; } = "eLiteGIS.Geoprocessing.GpTool";

    /// <summary>Factory interface.</summary>
    public string Factory { get; set; } = "eLiteGIS.Geoprocessing.IGpToolFactory";

    /// <summary>Description attribute (Russian and English text).</summary>
    public string Description { get; set; } = "eLiteGIS.Geoprocessing.GpDescriptionAttribute";

    /// <summary>Category attribute.</summary>
    public string Category { get; set; } = "eLiteGIS.Geoprocessing.GpCategoryAttribute";

    /// <summary>Icon attribute.</summary>
    public string Icon { get; set; } = "eLiteGIS.Geoprocessing.GpToolIconAttribute";
}
