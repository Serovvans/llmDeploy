# GPTool: выжимка инструкции заказчика

Источник: `docs/Инструкция создания GPtool.pdf` (15 страниц, скриншоты Visual Studio и
CoGIS). Справка для агентов раздела «Код»; где расходится с `docs/portal-design.md`,
прав дизайн. Имена типов взяты со скриншотов — сверить с настоящими DLL CoGIS.

## Как сотрудник делает это сейчас

1. Visual Studio 2022 → «Библиотека классов (Майкрософт)», C#, платформа **.NET 8.0**.
2. С сервера CoGIS (`/usr/elitegis/server/`) копирует в проект `eLiteGIS.Server.dll` и
   `Atemiko.eLiteMap.dll`; «Проект → Добавить ссылку на проект → Обзор» — ссылки на них.
3. NuGet: пакеты по задаче (в примере — `Npgsql` 9.0.3, драйвер PostgreSQL).
4. Переименовывает `Class1.cs` в `<ИмяПроекта>.cs`, пишет код.
5. «Сборка → Собрать решение»; ошибки с красным значком мешают, предупреждения нет.
6. Из `bin/Debug/net8.0/` копирует `<ИмяПроекта>.dll` на сервер в `/usr/elitegis/plugins/`.
   В этой папке уже лежат, например: `Npgsql.dll`, `Newtonsoft.Json.dll`,
   `Microsoft.ML.*.dll`, `FuzzySharp.dll`, `System.CodeDom.dll`,
   `Microsoft.Extensions.Logging.Abstractions.dll`,
   `Microsoft.Extensions.DependencyInjection.Abstractions.dll`,
   `System.Numerics.Tensors.dll`, `eLiteGIS.Geoprocessing.Cadastre.dll`, другие GPTool.
7. CoGIS Server → «Настройки → Глобальные настройки → Сервисы геообработки →
   Подключаемые .NET библиотеки → «+»»: путь к DLL-файлу
   (`/usr/elitegis/plugins/<Имя>.dll`) и имя класса (`<Пространство имён>.GpToolFactory`,
   например `AdressEmailGpTool.GpToolFactory`) → «Проверить» → «ОК».
8. Сервисы → новый геосервис → «Создание инструмента геообработки» → тип ищется в
   своей категории (в примере «Утилиты» → «Добавить контакт организации»).

## Устройство кода

```csharp
using Atemiko.eLiteMap.Geodatabase;   // подключение к БД (Workspace, WorkspaceName)
using eLiteGIS.Geoprocessing;         // GpTool, IGpToolFactory, атрибуты
using eLiteGIS.Utilities;             // UserException и др.
using Npgsql;
using System;

namespace AdressEmailGpTool               // своё имя; по нему — имя класса фабрики
{
    public sealed class GpToolFactory : IGpToolFactory   // «это никогда не меняем»
    {
        public Type[] GetToolTypes()
        {
            return new Type[] { typeof(AddMunicipalContactTool) };  // все инструменты проекта
        }
    }

    [GpToolIcon("fa fa-envelope")]        // иконка Font Awesome
    [GpCategory("Утилиты")]               // категория в списке инструментов
    [GpDescription("Добавить контакт организации", "Add Municipal Contact")]  // RU, EN
    public sealed class AddMunicipalContactTool : GpTool
    {
        [GpRequired]
        [GpDescription("Наименование организации", "Organization Name")]
        public string? NaimenovanieMO { get; set; }       // входной: public get; set

        [GpRequired]
        [GpDescription("Email-адрес", "Email Address")]
        public string? Email { get; set; }

        [GpRequired]
        [GpDescription("Целевая база данных (PostgreSQL workspace)", englishDescription: "PostgreSQL workspace")]
        public Workspace TargetWorkspace { get; set; }

        [GpDescription("Результат выполнения", "Execution result")]
        public string? Result { get; private set; }       // выходной: public get; private set

        public override void ValidateInput()
        {
            if (string.IsNullOrWhiteSpace(NaimenovanieMO))
                throw new UserException("Не задано наименование организации!");
            // ... ошибки ввода — UserException, фреймворк покажет сообщение
        }

        public override void InnerExecute()
        {
            string connectionString = GetPgConnectionString();
            using (var conn = new NpgsqlConnection(connectionString)) { /* ... */ }
            // ошибки БД не перехватываются — их обработает фреймворк
        }

        private string GetPgConnectionString()
        {
            if (TargetWorkspace == null) throw new UserException("TargetWorkspace не задан!");
            var wsName = TargetWorkspace.WorkspaceName;
            if (wsName == null) throw new UserException("Не удалось получить WorkspaceName!");
            var props = wsName.ConnectionProperties;        // словарь вида Dictionary<string, object>
            if (props == null) throw new UserException("Нет ConnectionProperties!");
            if (props.TryGetValue(WorkspaceName.PropertyNames.ConnectionString, out object connStrObj)
                && connStrObj is string s && !string.IsNullOrWhiteSpace(s))
                return s;
            // иначе Host, Port (по умолчанию "5432"), Database, User, Password из
            // WorkspaceName.PropertyNames.*; без Host / Database / User — UserException
            return $"Host={host};Port={port};Database={database};Username={user};Password={password};";
        }
    }
}
```

## Правила, которые портал может проверить без запуска

Кандидаты для проверки правил GPTool (§5.4 дизайна); точный список закрепляет контракт
после сверки с настоящими DLL.
1. В проекте ровно одна открытая фабрика, реализующая `IGpToolFactory`; её полное имя —
   то, что сотрудник укажет в CoGIS.
2. `GetToolTypes` возвращает `typeof(...)` всех неабстрактных наследников `GpTool`
   проекта и только их.
3. У инструмента есть `GpDescription` (RU и EN), `GpCategory`, `GpToolIcon`.
4. Входные параметры — открытые свойства с `get; set`; выходные — `get; private set`.
   У параметров есть `GpDescription`.
5. Ошибки ввода — `UserException` в `ValidateInput`, а не другие исключения.
6. Пространство имён задано (не глобальное).
7. Целевая платформа — .NET 8.

## Чего в инструкции нет

Полного списка типов параметров (кроме `string?` и `Workspace`), других атрибутов,
жизненного цикла `GpTool`, выходных параметров не-строкового типа, логирования, того,
как CoGIS грузит зависимости из `plugins/`. Всё это — из справочника API, который
`make cogis-sdk` строит по настоящим DLL, и из документации CoGIS в базе знаний.
