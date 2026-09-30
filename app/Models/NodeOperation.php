<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Relations\BelongsTo;

class NodeOperation extends BaseModel
{
    protected $guarded = ['id'];

    protected $hidden = ['id', 'runtime_context_id', 'actor_id', 'transport_fingerprint', 'request', 'attempt_uuid'];

    protected $attributes = ['status' => 'queued', 'attempts' => 0];

    protected function casts(): array
    {
        return ['request' => 'array', 'result' => 'array', 'expected_generation' => 'integer', 'last_node_sequence' => 'integer', 'cursor' => 'integer',
            'attempts' => 'integer', 'lease_expires_at' => 'immutable_datetime'];
    }

    public function runtimeContext(): BelongsTo
    {
        return $this->belongsTo(RuntimeContext::class);
    }

    public function isTerminal(): bool
    {
        return in_array($this->status, ['succeeded', 'failed', 'denied'], true);
    }
}
