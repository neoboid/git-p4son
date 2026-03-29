# Brief

The output of the preview sync step has become quite verbose, here is an example of a sync in a real project.
```powershell
# Previewing sync to CL 1205602
>  p4 sync -n //andreas.andersson-DC1-WS15-games-programmer/...@1205602
elapsed: 0:00:06.914998
[ok] 1603 files would be synced
>  git check-ignore --stdin
>  git log -1 --pretty=%H "--grep=: p4 sync //" 23159049d97c55a48b9fc9ba7a19da9f7f2168cd -- Discovery/Config/DefaultFeatureFlags.ini
>  git rev-parse --verify --quiet 23159049d97c55a48b9fc9ba7a19da9f7f2168cd:Discovery/Config/DefaultFeatureFlags.ini
>  git rev-parse --verify --quiet 1bce197c76291fed76436eda698daf01ecb48b17:Discovery/Config/DefaultFeatureFlags.ini
>  git log -1 --pretty=%H "--grep=: p4 sync //" 23159049d97c55a48b9fc9ba7a19da9f7f2168cd -- Discovery/Config/DefaultGameplayTags.ini
>  git rev-parse --verify --quiet 23159049d97c55a48b9fc9ba7a19da9f7f2168cd:Discovery/Config/DefaultGameplayTags.ini
>  git rev-parse --verify --quiet 1bce197c76291fed76436eda698daf01ecb48b17:Discovery/Config/DefaultGameplayTags.ini

... lots of lines like this

[ok] 170 writable files unchanged, skipping merge
```
(The full log listing output of all 1603 files can be found in `temp/verbose-output.log`)


The idea of logging the concrete commands run per file does not really work here. I would like it hide the commands
and list each file, and the information we get from the commands instead, as well as reor

```powershell
# Previewing sync to CL 1205602
>  p4 sync -n //andreas.andersson-DC1-WS15-games-programmer/...@1205602
elapsed: 0:00:06.914998
[ok] 1603 files would be synced

# Split files into tracked and ignored
>  git check-ignore --stdin
[ok] 170 files tracked and 1433 ignored

# Find files to merge post sync
Discovery/Config/DefaultFeatureFlags.ini

>  git log -1 --pretty=%H "--grep=: p4 sync //" 23159049d97c55a48b9fc9ba7a19da9f7f2168cd -- Discovery/Config/DefaultFeatureFlags.ini
>  git rev-parse --verify --quiet 23159049d97c55a48b9fc9ba7a19da9f7f2168cd:Discovery/Config/DefaultFeatureFlags.ini
>  git rev-parse --verify --quiet 1bce197c76291fed76436eda698daf01ecb48b17:Discovery/Config/DefaultFeatureFlags.ini
>  git log -1 --pretty=%H "--grep=: p4 sync //" 23159049d97c55a48b9fc9ba7a19da9f7f2168cd -- Discovery/Config/DefaultGameplayTags.ini
>  git rev-parse --verify --quiet 23159049d97c55a48b9fc9ba7a19da9f7f2168cd:Discovery/Config/DefaultGameplayTags.ini
>  git rev-parse --verify --quiet 1bce197c76291fed76436eda698daf01ecb48b17:Discovery/Config/DefaultGameplayTags.ini
```
