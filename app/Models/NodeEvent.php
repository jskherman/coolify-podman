<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Factories\HasFactory;

class NodeEvent extends BaseModel
{
    use HasFactory;

    protected $guarded = ['id'];

    protected $hidden = ['id', 'runtime_context_id', 'payload_hash'];

    protected function casts(): array
    {
        return ['sequence' => 'integer', 'payload' => 'array'];
    }
}
