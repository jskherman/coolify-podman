<?php

namespace App\Data;

use Closure;
use Illuminate\Support\Facades\Validator;
use Illuminate\Validation\ValidationException;

final readonly class ApplicationSpec
{
    /** @param array<string, mixed> $specification */
    private function __construct(private array $specification) {}

    /** @param array<string, mixed> $input */
    public static function fromArray(array $input): self
    {
        $defaults = [
            'profile' => 'prebuilt-v1', 'source' => ['type' => 'image'],
            'command' => [], 'entrypoint' => [], 'environment' => [],
            'health' => ['path' => '/', 'status' => 200, 'timeout_seconds' => 5],
            'limits' => ['memory_mib' => 128, 'cpu_millis' => 1000],
            'network' => ['internal' => true], 'volumes' => [], 'dependencies' => [],
            'restart_policy' => 'always', 'update_strategy' => 'recreate', 'migration_classification' => 'unknown',
            'ingress' => [], 'labels' => [], 'extensions' => [],
        ];
        $data = array_replace($defaults, $input);
        foreach (['source', 'health', 'limits', 'network'] as $field) {
            if (is_array($data[$field])) {
                $data[$field] = array_replace($defaults[$field], $data[$field]);
            }
        }
        foreach (['ports' => ['bind' => '127.0.0.1'], 'volumes' => ['read_only' => false]] as $field => $values) {
            if (is_array($data[$field] ?? null)) {
                foreach ($data[$field] as $index => $row) {
                    if (is_array($row)) {
                        $data[$field][$index] = array_replace($values, $row);
                    }
                }
            }
        }
        $safeText = static function (string $attribute, mixed $value, Closure $fail): void {
            if (is_string($value) && preg_match('/[\x00-\x1f\x7f$%]/', $value)) {
                $fail('Control characters and systemd interpolation are unsupported in the prebuilt-v1 profile.');
            }
        };
        Validator::make(['spec' => $data], [
            'spec' => ['required', 'array:'.implode(',', [...array_keys($defaults), 'schema_version', 'image', 'ports'])],
            'spec.schema_version' => ['required', 'integer', 'in:1'], 'spec.profile' => ['required', 'in:prebuilt-v1'],
            'spec.source' => ['required', 'array:type'], 'spec.source.type' => ['required', 'in:image'],
            'spec.image' => ['required', 'string', 'max:512', 'regex:~\A[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*@sha256:[a-f0-9]{64}\z~'],
            'spec.command' => ['present', 'array', 'list', 'max:64'], 'spec.command.*' => ['required', 'string', 'max:1024', $safeText],
            'spec.entrypoint' => ['present', 'array', 'list', 'max:16'], 'spec.entrypoint.*' => ['required', 'string', 'max:1024', $safeText],
            'spec.environment' => ['present', 'array', 'size:0'], 'spec.dependencies' => ['present', 'array', 'size:0'],
            'spec.ingress' => ['present', 'array', 'size:0'], 'spec.extensions' => ['present', 'array', 'size:0'],
            'spec.health' => ['required', 'array:path,status,timeout_seconds'],
            'spec.health.path' => ['required', 'string', 'max:256', 'regex:~\A/[a-zA-Z0-9/._-]*\z~'],
            'spec.health.status' => ['required', 'integer', 'between:200,299'],
            'spec.health.timeout_seconds' => ['required', 'integer', 'between:1,10'],
            'spec.limits' => ['required', 'array:memory_mib,cpu_millis'],
            'spec.limits.memory_mib' => ['required', 'integer', 'between:32,4096'],
            'spec.limits.cpu_millis' => ['required', 'integer', 'between:100,4000'],
            'spec.network' => ['required', 'array:internal'], 'spec.network.internal' => ['required', 'accepted'],
            'spec.ports' => ['required', 'array', 'list', 'size:1'], 'spec.ports.*' => ['required', 'array:bind,host,container'],
            'spec.ports.*.bind' => ['required', 'in:127.0.0.1'],
            'spec.ports.*.host' => ['required', 'integer', 'between:1024,65535'],
            'spec.ports.*.container' => ['required', 'integer', 'between:1,65535'],
            'spec.volumes' => ['present', 'array', 'list', 'max:8'], 'spec.volumes.*' => ['required', 'array:name,target,read_only'],
            'spec.volumes.*.name' => ['required', 'string', 'max:32', 'distinct:strict', 'regex:/\A[a-z][a-z0-9-]*\z/'],
            'spec.volumes.*.target' => ['required', 'string', 'max:256', 'distinct:strict', 'regex:~\A/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*\z~'],
            'spec.volumes.*.read_only' => ['required', 'boolean'],
            'spec.restart_policy' => ['required', 'in:always'], 'spec.update_strategy' => ['required', 'in:recreate'],
            'spec.migration_classification' => ['required', 'in:none,backward-compatible,forward-only,unknown'],
            'spec.labels' => ['present', 'array', 'list', 'max:32'], 'spec.labels.*' => ['required', 'array:name,value'],
            'spec.labels.*.name' => ['required', 'string', 'max:128', 'distinct:strict', 'regex:/\A[a-z][a-z0-9_.-]*\z/'],
            'spec.labels.*.value' => ['present', 'string', 'max:512', $safeText],
        ])->validate();
        foreach ($data['volumes'] as $index => $volume) {
            if (array_intersect(explode('/', $volume['target']), ['.', '..'])) {
                throw ValidationException::withMessages(["spec.volumes.$index.target" => 'Volume targets must be canonical absolute container paths.']);
            }
            $data['volumes'][$index]['read_only'] = (bool) $volume['read_only'];
        }
        foreach ($data['labels'] as $index => $label) {
            if (str_starts_with($label['name'], 'coolify.') || str_starts_with($label['name'], 'io.containers.')) {
                throw ValidationException::withMessages(["spec.labels.$index.name" => 'Runtime ownership and image-update labels are reserved.']);
            }
        }
        $data['schema_version'] = 1;
        $data['network']['internal'] = true;
        foreach (['limits' => ['memory_mib', 'cpu_millis'], 'health' => ['status', 'timeout_seconds']] as $field => $keys) {
            foreach ($keys as $key) {
                $data[$field][$key] = (int) $data[$field][$key];
            }
        }
        $data['ports'][0]['host'] = (int) $data['ports'][0]['host'];
        $data['ports'][0]['container'] = (int) $data['ports'][0]['container'];
        usort($data['volumes'], static fn (array $a, array $b): int => strcmp($a['name'], $b['name']));
        usort($data['labels'], static fn (array $a, array $b): int => strcmp($a['name'], $b['name']));

        return new self(self::canonicalize($data));
    }

    /** @return array<string, mixed> */
    public function toArray(): array
    {
        return $this->specification;
    }

    public function hash(): string
    {
        return hash('sha256', json_encode($this->specification, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS));
    }

    /** @param array<array-key, mixed> $value
     * @return array<array-key, mixed>
     */
    private static function canonicalize(array $value): array
    {
        foreach ($value as $key => $item) {
            if (is_array($item)) {
                $value[$key] = self::canonicalize($item);
            }
        }
        if (! array_is_list($value)) {
            ksort($value, SORT_STRING);
        }

        return $value;
    }
}
