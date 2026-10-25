using System;
using Atemiko.eLiteMap.Geodatabase;
using eLiteGIS.Geoprocessing;
using eLiteGIS.Utilities;
using Npgsql;

namespace {{namespace}}
{
    [GpToolIcon("fa fa-envelope")]
    [GpCategory("Утилиты")]
    [GpDescription("Добавить контакт организации", "Add organization contact")]
    public sealed class AddContactTool : GpTool
    {
        // Входные параметры: открытые get и set.
        [GpRequired]
        [GpDescription("Наименование организации", "Organization name")]
        public string? OrganizationName { get; set; }

        [GpRequired]
        [GpDescription("Email-адрес", "Email address")]
        public string? Email { get; set; }

        [GpRequired]
        [GpDescription("Подключение к базе", "Database connection")]
        public Workspace? TargetWorkspace { get; set; }

        // Выходной параметр: открытый get, закрытый set.
        [GpDescription("Результат выполнения", "Execution result")]
        public string? Result { get; private set; }

        // Ошибки ввода — UserException: CoGIS покажет текст пользователю.
        public override void ValidateInput()
        {
            if (string.IsNullOrWhiteSpace(OrganizationName))
            {
                throw new UserException("Не задано наименование организации.");
            }

            if (string.IsNullOrWhiteSpace(Email) || !Email.Contains('@'))
            {
                throw new UserException("Не задан или неверен email-адрес.");
            }
        }

        // Ошибки базы данных не перехватываются: их обработает CoGIS.
        public override void InnerExecute()
        {
            using var connection = new NpgsqlConnection(GetConnectionString());
            connection.Open();
            using var command = new NpgsqlCommand(
                "INSERT INTO contacts (organization, email) VALUES (@organization, @email)", connection);
            command.Parameters.AddWithValue("organization", OrganizationName!.Trim());
            command.Parameters.AddWithValue("email", Email!.Trim());
            command.ExecuteNonQuery();
            Result = $"Добавлен контакт организации «{OrganizationName.Trim()}».";
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
    }
}
