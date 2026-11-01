param(
    [string]$SnapshotPath = 'C:\Dev\tint-ppf-cutter\analysis\snapshots\fvd.mdb',
    [string]$OutputPath = 'C:\Dev\tint-ppf-cutter\analysis\catalog-schema.json'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Data
$snapshot = (Resolve-Path -LiteralPath $SnapshotPath).ProviderPath
$snapshotRoot = 'C:\Dev\tint-ppf-cutter\analysis\snapshots\'
if (-not $snapshot.StartsWith($snapshotRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'This script only reads workspace snapshots, never the live vendor database.'
}
$before = (Get-FileHash -LiteralPath $snapshot -Algorithm SHA256).Hash
$connection = New-Object System.Data.OleDb.OleDbConnection
$builder = New-Object System.Data.OleDb.OleDbConnectionStringBuilder
$builder.Provider = 'Microsoft.ACE.OLEDB.12.0'
$builder.DataSource = $snapshot
$builder['Mode'] = 'Read'
$builder['Persist Security Info'] = $false
# A password recovered from the application's own configuration may be supplied
# in the process environment. It is never printed or written to an output file.
if ($env:FVD_CATALOG_PASSWORD) {
    $builder['Jet OLEDB:Database Password'] = $env:FVD_CATALOG_PASSWORD
}
$connection.ConnectionString = $builder.ConnectionString
$tables = @()
try {
    $connection.Open()
    $tableSchema = $connection.GetOleDbSchemaTable([System.Data.OleDb.OleDbSchemaGuid]::Tables, @($null, $null, $null, 'TABLE'))
    $columnSchema = $connection.GetOleDbSchemaTable([System.Data.OleDb.OleDbSchemaGuid]::Columns, $null)
    foreach ($row in $tableSchema.Rows) {
        $name = [string]$row.TABLE_NAME
        if ($name.StartsWith('MSys', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $columns = @($columnSchema.Rows | Where-Object { $_.TABLE_NAME -eq $name } | Sort-Object ORDINAL_POSITION | ForEach-Object {
            [pscustomobject]@{
                name = [string]$_.COLUMN_NAME
                ordinal = [int]$_.ORDINAL_POSITION
                ole_db_type = [int]$_.DATA_TYPE
                nullable = [bool]$_.IS_NULLABLE
                max_length = if ($_.CHARACTER_MAXIMUM_LENGTH -is [DBNull]) { $null } else { [long]$_.CHARACTER_MAXIMUM_LENGTH }
            }
        })
        $command = $connection.CreateCommand()
        $command.CommandText = 'SELECT COUNT(*) FROM [' + $name.Replace(']', ']]') + ']'
        $count = [long]$command.ExecuteScalar()
        $tables += [pscustomobject]@{ name = $name; row_count = $count; columns = $columns }
        $command.Dispose()
    }
} finally {
    $connection.Dispose()
}
$after = (Get-FileHash -LiteralPath $snapshot -Algorithm SHA256).Hash
if ($before -ne $after) { throw 'Snapshot digest changed during read-only inspection.' }
$result = [pscustomobject]@{
    snapshot = $snapshot
    sha256 = $before
    provider = 'Microsoft.ACE.OLEDB.12.0'
    mode = 'Read'
    snapshot_unchanged = $true
    tables = @($tables | Sort-Object name)
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
$result.tables | Select-Object name, row_count, @{n='columns';e={$_.columns.name -join ', '}} | Format-Table -AutoSize -Wrap
