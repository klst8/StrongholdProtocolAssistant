$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime]

$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1

function Wait-Result($operation, [type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}

function Read-Image($encoded, $engine) {
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $writer = [Windows.Storage.Streams.DataWriter]::new($stream)
    try {
        $writer.WriteBytes([Convert]::FromBase64String($encoded))
        $null = Wait-Result ($writer.StoreAsync()) ([uint32])
        $null = $writer.DetachStream()
        $stream.Seek(0)
        $decoder = Wait-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-Result ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $result = Wait-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            foreach ($line in $result.Lines) {
                @{
                    text = $line.Text
                    words = @(foreach ($word in $line.Words) {
                        $rect = $word.BoundingRect
                        @{ text = $word.Text; x = $rect.X; y = $rect.Y; w = $rect.Width; h = $rect.Height }
                    })
                }
            }
        } finally {
            $bitmap.Dispose()
        }
    } finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('zh-Hans'))
if ($null -eq $engine) {
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
    @{ error = 'missing_zh_ocr'; languages = $languages } | ConvertTo-Json -Compress
    exit 1
}
@{ ready = $true; max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension } | ConvertTo-Json -Compress
while ($null -ne ($inputLine = [Console]::ReadLine())) {
    try {
        @{ lines = @(Read-Image $inputLine $engine) } | ConvertTo-Json -Compress -Depth 6
    } catch {
        @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    }
}
