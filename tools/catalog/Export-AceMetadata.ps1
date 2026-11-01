param(
    [Parameter(Mandatory=$true)][string]$SnapshotPath,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$snapshotRoot = [IO.Path]::GetFullPath((Join-Path $workspaceRoot 'analysis\snapshots')) + [IO.Path]::DirectorySeparatorChar
$snapshot = (Resolve-Path -LiteralPath $SnapshotPath).ProviderPath
if (-not $snapshot.StartsWith($snapshotRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Only workspace snapshots are supported.'
}
$source = @'
using System;
using System.Collections.Generic;
using System.Data;
using System.Data.OleDb;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;

public static class FilmCatalogAceReader {
    public static string Export(string snapshot, string output, string password) {
        string[] tables = {"CarPattern", "CarPatternType", "CarMake", "VehicleModel", "VehicleTrimLevel", "CarModel", "CarYear", "AuthorsForPatterns", "Regions", "RegionsStates", "ImportRegionsForPatterns", "Instructions", "Links"};
        var serializer = new JavaScriptSerializer();
        var builder = new OleDbConnectionStringBuilder();
        builder.Provider = "Microsoft.ACE.OLEDB.12.0";
        builder.DataSource = snapshot;
        builder["Mode"] = "Read";
        builder["Persist Security Info"] = false;
        if (!String.IsNullOrEmpty(password)) builder["Jet OLEDB:Database Password"] = password;
        Directory.CreateDirectory(output);
        var result = new List<Dictionary<string,object>>();
        using (var connection = new OleDbConnection(builder.ConnectionString)) {
            connection.Open();
            foreach (string table in tables) {
                var item = new Dictionary<string,object>();
                item["name"] = table;
                using (var countCommand = connection.CreateCommand()) {
                    countCommand.CommandText = "SELECT COUNT(*) FROM [" + table + "]";
                    item["declared_rows"] = Convert.ToInt64(countCommand.ExecuteScalar());
                }
                var columns = new List<Dictionary<string,object>>();
                long rows = 0;
                using (var command = connection.CreateCommand()) {
                    command.CommandText = "SELECT * FROM [" + table + "]";
                    using (var reader = command.ExecuteReader()) {
                        for (int i=0; i<reader.FieldCount; i++) {
                            var column = new Dictionary<string,object>();
                            column["name"] = reader.GetName(i);
                            column["dotnet_type"] = reader.GetFieldType(i).FullName;
                            columns.Add(column);
                        }
                        using (var stream = new StreamWriter(Path.Combine(output, table + ".jsonl"), false, new UTF8Encoding(false))) {
                            while (reader.Read()) {
                                object[] values = new object[reader.FieldCount];
                                for (int i=0; i<reader.FieldCount; i++) {
                                    object value = reader.GetValue(i);
                                    if (value == DBNull.Value) value = null;
                                    else if (value is DateTime) value = ((DateTime)value).ToString("o", CultureInfo.InvariantCulture);
                                    else if (value is Guid) value = value.ToString();
                                    values[i] = value;
                                }
                                stream.WriteLine(serializer.Serialize(values));
                                rows++;
                            }
                        }
                    }
                }
                item["columns"] = columns;
                item["exported_rows"] = rows;
                var primaryKeys = new List<string>();
                var primarySchema = connection.GetOleDbSchemaTable(OleDbSchemaGuid.Primary_Keys, new object[]{null,null,table});
                if (primarySchema != null) foreach (DataRow row in primarySchema.Rows) primaryKeys.Add(Convert.ToString(row["COLUMN_NAME"]));
                item["source_primary_keys"] = primaryKeys;
                result.Add(item);
            }
        }
        string json = serializer.Serialize(result);
        File.WriteAllText(Path.Combine(output, "schema.json"), json, new UTF8Encoding(false));
        return json;
    }
}
'@
Add-Type -TypeDefinition $source -ReferencedAssemblies System.Data,System.Web.Extensions,System.Xml
$result = [FilmCatalogAceReader]::Export($snapshot, $OutputDirectory, $env:FVD_CATALOG_PASSWORD)
Remove-Item Env:FVD_CATALOG_PASSWORD -ErrorAction SilentlyContinue
$result
