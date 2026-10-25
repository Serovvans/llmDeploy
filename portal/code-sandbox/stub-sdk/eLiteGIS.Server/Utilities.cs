namespace eLiteGIS.Utilities;

/// <summary>Error that CoGIS shows to the user as is.</summary>
public class UserException : Exception
{
    /// <summary>Creates the error.</summary>
    /// <param name="message">Text for the user.</param>
    public UserException(string message)
        : base(message)
    {
    }
}
