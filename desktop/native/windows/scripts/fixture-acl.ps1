param([Parameter(Mandatory=$true)][string]$OwnedDir)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($env:GITHUB_ACTIONS -ne 'true'){throw 'Disposable CI fixture required'}
$root=[IO.Path]::GetFullPath($OwnedDir)
$temporary=[IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\','/')+[IO.Path]::DirectorySeparatorChar
if(-not $root.StartsWith($temporary,[StringComparison]::OrdinalIgnoreCase)){throw 'Only this runner temporary subtree is allowed'}
if(((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Fixture must not redirect to another directory'}
$owner=Get-Content -Raw -LiteralPath (Join-Path $root 'owner.json')|ConvertFrom-Json
if($owner.schema -ne 'svi-desktop-live-environment-v1' -or [IO.Path]::GetFullPath($owner.run_dir) -ine $root -or $owner.database -notlike 'smartvoiceinput_test_desktop_*' -or (Test-Path (Join-Path $root 'pg-data'))){throw 'Fresh owned desktop fixture required'}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$user=$identity.User
$rights=[Security.AccessControl.FileSystemRights]::FullControl
$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
function Has-UserAccess($Acl){
    foreach($entry in $Acl.Access){
        if($entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $user.Value -and $entry.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and ($entry.FileSystemRights -band $rights) -eq $rights -and ($entry.InheritanceFlags -band $inherit) -eq $inherit){return $true}
    }
    return $false
}
$acl=Get-Acl -LiteralPath $root
$before=Has-UserAccess $acl
$rule=[Security.AccessControl.FileSystemAccessRule]::new($user,$rights,$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $root -AclObject $acl
$after=Has-UserAccess (Get-Acl -LiteralPath $root)
if(-not $after){throw 'Exact test user access was not independently read back'}
$principal=[Security.Principal.WindowsPrincipal]::new($identity)
@{scope='new owned disposable fixture only';explicit_current_user_before=$before;explicit_current_user_after=$after;inherited_to_children=$true;parent_admin_group_enabled=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator);identity_or_credentials_recorded=$false}|ConvertTo-Json -Compress
