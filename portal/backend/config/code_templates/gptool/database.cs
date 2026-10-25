        [GpRequired]
        [GpDescription("Подключение к базе", "Database connection")]
        public Workspace? TargetWorkspace { get; set; }

        private NpgsqlConnection OpenConnection()
        {
            var connection = new NpgsqlConnection(GetConnectionString());
            connection.Open();
            return connection;
        }

        private string GetConnectionString()
        {
            var properties = TargetWorkspace?.WorkspaceName?.ConnectionProperties
                ?? throw new UserException("Не задано подключение к базе данных.");
            if (properties.TryGetValue(WorkspaceName.PropertyNames.ConnectionString, out var value)
                && value is string connectionString
                && !string.IsNullOrWhiteSpace(connectionString))
            {
                return connectionString;
            }

            string? Property(string name) => properties.TryGetValue(name, out var item) ? item?.ToString() : null;

            var builder = new NpgsqlConnectionStringBuilder
            {
                Host = Property(WorkspaceName.PropertyNames.Host),
                Port = int.TryParse(Property(WorkspaceName.PropertyNames.Port), out var port) ? port : 5432,
                Database = Property(WorkspaceName.PropertyNames.Database),
                Username = Property(WorkspaceName.PropertyNames.User),
                Password = Property(WorkspaceName.PropertyNames.Password),
            };
            if (string.IsNullOrEmpty(builder.Host) || string.IsNullOrEmpty(builder.Database) || string.IsNullOrEmpty(builder.Username))
            {
                throw new UserException("В подключении к базе нет сервера, базы данных или пользователя.");
            }

            return builder.ConnectionString;
        }
