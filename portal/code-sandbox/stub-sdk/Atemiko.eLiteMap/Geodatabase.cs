namespace Atemiko.eLiteMap.Geodatabase;

/// <summary>Database connection selected by the user of a tool.</summary>
public abstract class Workspace
{
    /// <summary>Connection description.</summary>
    public WorkspaceName? WorkspaceName { get; }
}

/// <summary>Connection description of a workspace.</summary>
public class WorkspaceName
{
    /// <summary>Connection properties by the names from <see cref="PropertyNames"/>.</summary>
    public IDictionary<string, object>? ConnectionProperties { get; }

    /// <summary>Keys of <see cref="ConnectionProperties"/>.</summary>
    public static class PropertyNames
    {
        /// <summary>Full connection string.</summary>
        public const string ConnectionString = "ConnectionString";

        /// <summary>Server host.</summary>
        public const string Host = "Host";

        /// <summary>Server port.</summary>
        public const string Port = "Port";

        /// <summary>Database name.</summary>
        public const string Database = "Database";

        /// <summary>User name.</summary>
        public const string User = "User";

        /// <summary>Password.</summary>
        public const string Password = "Password";
    }
}
