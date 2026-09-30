<?php

namespace App\Services;

use App\Data\ApplicationSpec;
use Illuminate\Support\Facades\Validator;

class QuadletCompiler
{
    public const VERSION = 'prebuilt-v1.0';

    /** @return array<string, string> */
    public function compile(ApplicationSpec $specification, string $resourceId): array
    {
        Validator::make(['resource_id' => $resourceId], ['resource_id' => ['required', 'uuid', 'lowercase']])->validate();
        $spec = $specification->toArray();
        $name = 'coolify-'.$resourceId;
        $header = "# Coolify managed resource $resourceId\n# Compiler ".self::VERSION."\n# Spec ".$specification->hash()."\n";
        $files = [
            "$name.network" => $header."[Network]\nNetworkName=$name\nInternal=true\nLabel=coolify.managed=true\nLabel=coolify.resource=$resourceId\n",
        ];
        $container = $header."[Unit]\nDescription=Coolify application $resourceId\n\n[Container]\nImage={$spec['image']}\nContainerName=$name\nNetwork=$name.network\n";
        foreach ($spec['ports'] as $port) {
            $container .= "PublishPort={$port['bind']}:{$port['host']}:{$port['container']}\n";
        }
        $container .= "Label=coolify.managed=true\nLabel=coolify.resource=$resourceId\n";
        foreach ($spec['labels'] as $label) {
            $container .= 'Label='.$this->quote($label['name'].'='.$label['value'])."\n";
        }
        if ($spec['entrypoint'] !== []) {
            $container .= 'Entrypoint='.json_encode($spec['entrypoint'], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS)."\n";
        }
        if ($spec['command'] !== []) {
            $container .= 'Exec='.implode(' ', array_map($this->quote(...), $spec['command']))."\n";
        }
        foreach ($spec['volumes'] as $volume) {
            $volumeName = $name.'-'.$volume['name'];
            $files["$volumeName.volume"] = $header."[Volume]\nVolumeName=$volumeName\nLabel=coolify.managed=true\nLabel=coolify.resource=$resourceId\n";
            $container .= "Volume=$volumeName.volume:{$volume['target']}:".($volume['read_only'] ? 'ro' : 'rw')."\n";
        }
        $quota = intdiv($spec['limits']['cpu_millis'], 10);
        $fraction = $spec['limits']['cpu_millis'] % 10;
        $cpuQuota = $quota.($fraction ? ".{$fraction}" : '').'%';
        $container .= "\n[Service]\nRestart=always\nTimeoutStartSec=120\nMemoryMax={$spec['limits']['memory_mib']}M\nCPUQuota=$cpuQuota\n\n[Install]\nWantedBy=default.target\n";
        $files["$name.container"] = $container;
        ksort($files, SORT_STRING);

        return $files;
    }

    private function quote(string $value): string
    {
        return json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS);
    }
}
