using System.Collections.Immutable;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace CodeSandbox;

/// <summary><c>reference.json</c>: public API of the CoGIS libraries and of frequent .NET namespaces (portal-api.md §7.5.14).</summary>
internal sealed record ApiReference(int Format, string SdkVersion, IReadOnlyList<ApiType> Types)
{
    /// <summary>File name inside the index folder.</summary>
    public const string FileName = "reference.json";

    private static readonly HashSet<string> DotnetNamespaces = new(StringComparer.Ordinal)
    {
        "System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Text", "System.Threading.Tasks",
    };

    private static readonly string[] MemberKindOrder = ["constructor", "method", "property", "field", "event", "enum_value"];

    private static readonly SymbolDisplayFormat NameFormat = new(
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameOnly,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters | SymbolDisplayGenericsOptions.IncludeVariance,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers);

    private static readonly SymbolDisplayFormat TypeUseFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypes,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        miscellaneousOptions: Miscellaneous);

    private static readonly SymbolDisplayFormat DelegateFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameOnly,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters | SymbolDisplayGenericsOptions.IncludeVariance
            | SymbolDisplayGenericsOptions.IncludeTypeConstraints,
        memberOptions: SymbolDisplayMemberOptions.IncludeAccessibility | SymbolDisplayMemberOptions.IncludeType
            | SymbolDisplayMemberOptions.IncludeParameters | SymbolDisplayMemberOptions.IncludeRef,
        delegateStyle: SymbolDisplayDelegateStyle.NameAndSignature,
        parameterOptions: ParameterOptions,
        kindOptions: SymbolDisplayKindOptions.IncludeTypeKeyword,
        miscellaneousOptions: Miscellaneous);

    private static readonly SymbolDisplayFormat MemberFormat = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypes,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters | SymbolDisplayGenericsOptions.IncludeTypeConstraints,
        memberOptions: SymbolDisplayMemberOptions.IncludeAccessibility | SymbolDisplayMemberOptions.IncludeModifiers
            | SymbolDisplayMemberOptions.IncludeType | SymbolDisplayMemberOptions.IncludeParameters
            | SymbolDisplayMemberOptions.IncludeRef | SymbolDisplayMemberOptions.IncludeConstantValue,
        parameterOptions: ParameterOptions,
        propertyStyle: SymbolDisplayPropertyStyle.ShowReadWriteDescriptor,
        kindOptions: SymbolDisplayKindOptions.IncludeMemberKeyword,
        miscellaneousOptions: Miscellaneous);

    private const SymbolDisplayMiscellaneousOptions Miscellaneous =
        SymbolDisplayMiscellaneousOptions.UseSpecialTypes
        | SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers
        | SymbolDisplayMiscellaneousOptions.IncludeNullableReferenceTypeModifier;

    private const SymbolDisplayParameterOptions ParameterOptions =
        SymbolDisplayParameterOptions.IncludeType | SymbolDisplayParameterOptions.IncludeName
        | SymbolDisplayParameterOptions.IncludeDefaultValue | SymbolDisplayParameterOptions.IncludeParamsRefOut
        | SymbolDisplayParameterOptions.IncludeExtensionThis;

    /// <summary>Reads the public types of the <c>cogis</c> libraries and of the frequent .NET namespaces from metadata.</summary>
    public static ApiReference Build(string sdkVersion, IReadOnlyList<(SdkFile File, byte[] Image)> sdkImages, ReferencePack referencePack)
    {
        var cogis = new List<MetadataReference>();
        var others = new List<MetadataReference>();
        foreach (var (file, image) in sdkImages.Where(item => item.File.Kind != SdkKinds.Framework))
        {
            var reference = MetadataReference.CreateFromImage(image, filePath: file.FileName);
            (file.Kind == SdkKinds.Cogis ? cogis : others).Add(reference);
        }

        var compilation = CSharpCompilation.Create("index", references: referencePack.References.Concat(cogis).Concat(others));
        var types = new List<ApiType>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var reference in cogis)
        {
            if (compilation.GetAssemblyOrModuleSymbol(reference) is IAssemblySymbol assembly)
            {
                Collect(assembly.GlobalNamespace, "cogis", _ => true, types, seen);
            }
        }

        foreach (var reference in referencePack.References)
        {
            if (compilation.GetAssemblyOrModuleSymbol(reference) is IAssemblySymbol assembly)
            {
                Collect(assembly.GlobalNamespace, "dotnet", DotnetNamespaces.Contains, types, seen);
            }
        }

        var ordered = types
            .OrderBy(type => type.Namespace, StringComparer.Ordinal)
            .ThenBy(type => type.Name, StringComparer.Ordinal)
            .ThenBy(type => type.Signature, StringComparer.Ordinal)
            .ToList();
        return new ApiReference(1, sdkVersion, ordered);
    }

    private static void Collect(INamespaceSymbol ns, string origin, Func<string, bool> includeNamespace, List<ApiType> types, HashSet<string> seen)
    {
        var name = ns.IsGlobalNamespace ? string.Empty : ns.ToDisplayString();
        if (includeNamespace(name))
        {
            foreach (var type in ns.GetTypeMembers().Where(type => type.DeclaredAccessibility == Accessibility.Public))
            {
                AddType(type, name, type.Name, origin, types, seen);
            }
        }

        foreach (var child in ns.GetNamespaceMembers())
        {
            Collect(child, origin, includeNamespace, types, seen);
        }
    }

    private static void AddType(INamedTypeSymbol type, string ns, string name, string origin, List<ApiType> types, HashSet<string> seen)
    {
        if (!seen.Add($"{ns}|{name}`{type.Arity}"))
        {
            return;
        }

        types.Add(new ApiType(ns, name, Kind(type), origin, TypeSignature(type), Members(type)));
        foreach (var nested in type.GetTypeMembers().Where(nested => nested.DeclaredAccessibility == Accessibility.Public))
        {
            AddType(nested, ns, $"{name}.{nested.Name}", origin, types, seen);
        }
    }

    private static string Kind(INamedTypeSymbol type) => type.TypeKind switch
    {
        TypeKind.Struct => "struct",
        TypeKind.Interface => "interface",
        TypeKind.Enum => "enum",
        TypeKind.Delegate => "delegate",
        _ => "class",
    };

    /// <summary>C# declaration with type parameters, base type, interfaces and constraints.</summary>
    internal static string TypeSignature(INamedTypeSymbol type)
    {
        if (type.TypeKind == TypeKind.Delegate)
        {
            return "public " + type.ToDisplayString(DelegateFormat);
        }

        var parts = new List<string> { "public" };
        if (type.TypeKind == TypeKind.Class)
        {
            if (type.IsStatic)
            {
                parts.Add("static");
            }
            else if (type.IsAbstract)
            {
                parts.Add("abstract");
            }
            else if (type.IsSealed)
            {
                parts.Add("sealed");
            }
        }
        else if (type.TypeKind == TypeKind.Struct)
        {
            if (type.IsReadOnly)
            {
                parts.Add("readonly");
            }

            if (type.IsRefLikeType)
            {
                parts.Add("ref");
            }
        }

        parts.Add(type.IsRecord ? (type.TypeKind == TypeKind.Struct ? "record struct" : "record") : Kind(type));
        var signature = $"{string.Join(' ', parts)} {type.ToDisplayString(NameFormat)}";

        var bases = new List<string>();
        if (type.TypeKind == TypeKind.Enum)
        {
            if (type.EnumUnderlyingType is { SpecialType: not SpecialType.System_Int32 } underlying)
            {
                bases.Add(underlying.ToDisplayString(TypeUseFormat));
            }
        }
        else
        {
            if (type.TypeKind == TypeKind.Class && !type.IsStatic && type.BaseType is { SpecialType: not SpecialType.System_Object } baseType)
            {
                bases.Add(baseType.ToDisplayString(TypeUseFormat));
            }

            bases.AddRange(type.Interfaces.Select(iface => iface.ToDisplayString(TypeUseFormat)));
        }

        if (bases.Count > 0)
        {
            signature += " : " + string.Join(", ", bases);
        }

        foreach (var parameter in type.TypeParameters)
        {
            var constraints = Constraints(parameter);
            if (constraints.Count > 0)
            {
                signature += $" where {parameter.Name} : {string.Join(", ", constraints)}";
            }
        }

        return signature;
    }

    private static List<string> Constraints(ITypeParameterSymbol parameter)
    {
        var result = new List<string>();
        if (parameter.HasReferenceTypeConstraint)
        {
            result.Add(parameter.ReferenceTypeConstraintNullableAnnotation == NullableAnnotation.Annotated ? "class?" : "class");
        }
        else if (parameter.HasUnmanagedTypeConstraint)
        {
            result.Add("unmanaged");
        }
        else if (parameter.HasValueTypeConstraint)
        {
            result.Add("struct");
        }
        else if (parameter.HasNotNullConstraint)
        {
            result.Add("notnull");
        }

        result.AddRange(parameter.ConstraintTypes.Select(constraint => constraint.ToDisplayString(TypeUseFormat)));
        if (parameter.HasConstructorConstraint)
        {
            result.Add("new()");
        }

        return result;
    }

    private static List<ApiMember> Members(INamedTypeSymbol type)
    {
        if (type.TypeKind == TypeKind.Delegate)
        {
            return [];
        }

        var members = new List<ApiMember>();
        foreach (var member in type.GetMembers())
        {
            if (member.IsImplicitlyDeclared
                || member.DeclaredAccessibility is not (Accessibility.Public or Accessibility.Protected or Accessibility.ProtectedOrInternal))
            {
                continue;
            }

            var kind = member switch
            {
                IMethodSymbol { MethodKind: MethodKind.Constructor } => "constructor",
                IMethodSymbol { MethodKind: MethodKind.Ordinary or MethodKind.UserDefinedOperator or MethodKind.Conversion } => "method",
                IPropertySymbol => "property",
                IFieldSymbol when type.TypeKind == TypeKind.Enum => "enum_value",
                IFieldSymbol => "field",
                IEventSymbol => "event",
                _ => null,
            };
            if (kind is not null)
            {
                members.Add(new ApiMember(kind, kind == "constructor" ? type.Name : member.Name, member.ToDisplayString(MemberFormat)));
            }
        }

        return members
            .OrderBy(member => Array.IndexOf(MemberKindOrder, member.Kind))
            .ThenBy(member => member.Name, StringComparer.Ordinal)
            .ThenBy(member => member.Signature, StringComparer.Ordinal)
            .ToList();
    }
}

/// <summary>Type of the reference: <c>name</c> without arity, nested through a dot.</summary>
internal sealed record ApiType(string Namespace, string Name, string Kind, string Origin, string Signature, IReadOnlyList<ApiMember> Members);

/// <summary>Public or protected member of a type.</summary>
internal sealed record ApiMember(string Kind, string Name, string Signature);
