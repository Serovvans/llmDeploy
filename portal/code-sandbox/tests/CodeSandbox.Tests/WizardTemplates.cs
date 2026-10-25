using System.Text.RegularExpressions;

namespace CodeSandbox.Tests;

/// <summary>Wizard parameter of a test form.</summary>
internal sealed record WizardParameter(string Name, string Type, bool Required, string TitleRu, string TitleEn);

/// <summary>Wizard tool of a test form; the class name is given (its derivation is the backend's).</summary>
internal sealed record WizardTool(
    string ClassName, string TitleRu, string TitleEn, string Category, string Icon, bool UsesDatabase,
    IReadOnlyList<WizardParameter> Inputs, IReadOnlyList<WizardParameter> Outputs);

/// <summary>
/// Minimal renderer of the wizard templates by portal-api.md §7.5.4: one pass of <c>{{name}}</c>,
/// fragments joined with a line break; a fragment file loses its final line break.
/// </summary>
internal static partial class WizardTemplates
{
    [GeneratedRegex(@"\{\{([a-z_]+)\}\}")]
    private static partial Regex Placeholder();

    [GeneratedRegex(@"id:\s*(\w+),.*csharp:\s*""([^""]+)"",.*using:\s*(null|""[^""]*""),.*required_check:\s*(null|""[^""]*"")")]
    private static partial Regex TypeLine();

    [GeneratedRegex(@"required_message:\s*""([^""]*)""")]
    private static partial Regex RequiredMessage();

    [GeneratedRegex(@"^database:\s*\{\s*usings:\s*\[([^\]]*)\]", RegexOptions.Multiline)]
    private static partial Regex DatabaseUsings();

    /// <summary>Files of the skeleton and whether the database packages are needed.</summary>
    public static (List<(string Path, string Content)> Files, bool UsesDatabase) Render(string ns, IReadOnlyList<WizardTool> tools)
    {
        var yaml = Read("gptool/wizard.yaml");
        var types = TypeLine().Matches(yaml).ToDictionary(
            match => match.Groups[1].Value,
            match => (CSharp: match.Groups[2].Value, Using: Unquote(match.Groups[3].Value), Check: Unquote(match.Groups[4].Value)));
        var requiredMessage = RequiredMessage().Match(yaml).Groups[1].Value;
        var databaseUsings = DatabaseUsings().Match(yaml).Groups[1].Value
            .Split(',').Select(item => item.Trim().Trim('"')).Where(item => item.Length > 0).ToList();

        var files = new List<(string, string)>
        {
            ("GpToolFactory.cs", Fill(Read("gptool/factory.cs"), new()
            {
                ["namespace"] = ns,
                ["tool_types"] = string.Join(", ", tools.Select(tool => $"typeof({tool.ClassName})")),
            })),
        };
        foreach (var tool in tools)
        {
            var usings = tool.Inputs.Concat(tool.Outputs).Select(parameter => types[parameter.Type].Using).OfType<string>()
                .Concat(tool.UsesDatabase ? databaseUsings : [])
                .Distinct().Order(StringComparer.Ordinal).Select(name => $"using {name};");
            var inputs = tool.Inputs.Select(parameter => Fill(Fragment("gptool/input.cs"), new()
            {
                ["required"] = parameter.Required ? Fragment("gptool/required.cs") : string.Empty,
                ["title_ru"] = Literal(parameter.TitleRu),
                ["title_en"] = Literal(parameter.TitleEn),
                ["type"] = types[parameter.Type].CSharp,
                ["name"] = parameter.Name,
            }));
            var outputs = tool.Outputs.Select(parameter => Fill(Fragment("gptool/output.cs"), new()
            {
                ["title_ru"] = Literal(parameter.TitleRu),
                ["title_en"] = Literal(parameter.TitleEn),
                ["type"] = types[parameter.Type].CSharp,
                ["name"] = parameter.Name,
            }));
            var validate = tool.Inputs.Where(parameter => parameter.Required && types[parameter.Type].Check is not null)
                .Select(parameter => Fill(Fragment("gptool/validate.cs"), new()
                {
                    ["condition"] = types[parameter.Type].Check!.Replace("{name}", parameter.Name, StringComparison.Ordinal),
                    ["message"] = Literal(requiredMessage.Replace("{title}", parameter.TitleRu, StringComparison.Ordinal)),
                }));
            files.Add(($"{tool.ClassName}.cs", Fill(Read("gptool/tool.cs"), new()
            {
                ["namespace"] = ns,
                ["usings"] = string.Join("\n", usings),
                ["class_name"] = tool.ClassName,
                ["icon"] = Literal(tool.Icon),
                ["category"] = Literal(tool.Category),
                ["title_ru"] = Literal(tool.TitleRu),
                ["title_en"] = Literal(tool.TitleEn),
                ["inputs"] = string.Join("\n", inputs),
                ["outputs"] = string.Join("\n", outputs),
                ["validate"] = string.Join("\n", validate),
                ["database"] = tool.UsesDatabase ? Fragment("gptool/database.cs") : string.Empty,
            })));
        }

        return (files, tools.Any(tool => tool.UsesDatabase));
    }

    /// <summary>Files of the example project.</summary>
    public static List<(string Path, string Content)> Example(string ns) =>
        Directory.GetFiles(Path.Combine(TestSdk.TemplatesDir, "example"), "*.cs")
            .Order(StringComparer.Ordinal)
            .Select(path => (Path.GetFileName(path), Fill(File.ReadAllText(path), new() { ["namespace"] = ns })))
            .ToList();

    private static string Fill(string template, Dictionary<string, string> values) =>
        Placeholder().Replace(template, match => values.TryGetValue(match.Groups[1].Value, out var value)
            ? value
            : throw new InvalidOperationException($"Unknown placeholder {match.Value}"));

    private static string Literal(string text) => $"\"{text.Replace("\\", "\\\\").Replace("\"", "\\\"")}\"";

    private static string Read(string relative) => File.ReadAllText(Path.Combine(TestSdk.TemplatesDir, relative));

    private static string Fragment(string relative)
    {
        var text = Read(relative);
        return text.EndsWith('\n') ? text[..^1] : text;
    }

    private static string? Unquote(string value) => value == "null" ? null : value.Trim('"');
}
