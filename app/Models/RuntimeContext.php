<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class RuntimeContext extends BaseModel
{
    use HasFactory;

    protected $guarded = ['id'];

    protected $hidden = ['executor_private_key_id', 'ssh_host_key', 'executor_private_key', 'server'];

    protected function casts(): array
    {
        return [
            'execution_uid' => 'integer', 'execution_gid' => 'integer',
            'storage' => 'array', 'versions' => 'array', 'capabilities' => 'array',
            'probed_at' => 'immutable_datetime',
            'controller_epoch' => 'integer', 'policy_version' => 'integer',
            'event_cursors' => 'array',
        ];
    }

    public function server(): BelongsTo
    {
        return $this->belongsTo(Server::class);
    }

    public static function connectionFingerprint(Server $server): string
    {
        return hash('sha256', json_encode([$server->id, $server->ip, $server->port, $server->user, $server->private_key_id], JSON_THROW_ON_ERROR));
    }

    public function executorPrivateKey(): BelongsTo
    {
        return $this->belongsTo(PrivateKey::class, 'executor_private_key_id');
    }

    public function transportFingerprint(): string
    {
        return hash('sha256', json_encode([
            self::connectionFingerprint($this->server), $this->executor_private_key_id,
            $this->execution_uid, $this->execution_gid, $this->manager, $this->identity_hash,
            $this->executorPrivateKey?->fingerprint, $this->ssh_host_key,
            $this->controller_epoch, $this->policy_version,
        ], JSON_THROW_ON_ERROR));
    }
}
