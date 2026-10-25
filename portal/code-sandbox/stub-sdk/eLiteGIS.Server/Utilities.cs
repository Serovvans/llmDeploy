namespace eLiteGIS.Utilities;

/// <summary>Russian and English description of a tool or a parameter.</summary>
[AttributeUsage(AttributeTargets.Class | AttributeTargets.Property)]
public class GpDescriptionAttribute : Attribute
{
    /// <summary>Creates the description.</summary>
    /// <param name="russianDescription">Russian text.</param>
    /// <param name="englishDescription">English text.</param>
    public GpDescriptionAttribute(string russianDescription, string englishDescription)
    {
    }
}

/// <summary>Font Awesome icon of a tool.</summary>
[AttributeUsage(AttributeTargets.Class)]
public class GpToolIconAttribute : Attribute
{
    /// <summary>Creates the icon.</summary>
    /// <param name="cssIcon">Font Awesome classes, for example <c>fa fa-envelope</c>.</param>
    public GpToolIconAttribute(string cssIcon)
    {
    }
}

/// <summary>Marks a required input parameter.</summary>
[AttributeUsage(AttributeTargets.Property)]
public class GpRequiredAttribute : Attribute
{
}
