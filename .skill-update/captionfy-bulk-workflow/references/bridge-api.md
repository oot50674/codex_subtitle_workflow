# Bridge API commands

Use PowerShell Core.

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$bridge = 'D:\Playground\chrome-extension\captionfy-bulk-uploader\captionfy_bridge.py'
$token = '<user-provided-token>'
```

Check health:

```powershell
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -Headers $headers
```

Queue one subtitle:

```powershell
python $bridge enqueue `
  --file 'D:\path\video.ko.srt' `
  --video-id 'XXXXXXXXXXX' `
  --language ko `
  --status Finished `
  --visibility public `
  --credit not_anonymous `
  --collaboration not_collaborative `
  --token $token
```

Inspect all jobs:

```powershell
python $bridge status --token $token
```

For a batch, enqueue sequentially and save the returned job IDs. Poll status every two seconds and filter by those IDs or exact video IDs. Stop when each job is `completed` or `failed`. Report each failure's `error` without automatically retrying an ambiguous completed request.

The manager extension must show the bridge as connected and `API 작업 자동 처리` must be enabled. HTTP `204` from `/api/jobs/next` means the queue is empty.
