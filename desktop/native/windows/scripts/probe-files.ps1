function Write-NewJson($Value, [string]$File) {
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $stream = [System.IO.File]::Open($File, [System.IO.FileMode]::CreateNew)
    try {
        $bytes = $encoding.GetBytes(($Value | ConvertTo-Json -Depth 40))
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}
