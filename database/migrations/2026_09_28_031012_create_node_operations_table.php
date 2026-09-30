<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('node_operations', function (Blueprint $table) {
            $table->id();
            $table->uuid('uuid')->unique();
            $table->foreignId('runtime_context_id')->constrained()->restrictOnDelete();
            $table->unsignedBigInteger('actor_id');
            $table->uuid('idempotency_key');
            $table->uuid('resource_id');
            $table->uuid('target_operation_id')->nullable();
            $table->string('action');
            $table->unsignedBigInteger('expected_generation');
            $table->string('transport_fingerprint', 64);
            $table->json('request');
            $table->string('status')->default('queued')->index();
            $table->json('result')->nullable();
            $table->string('error_code')->nullable();
            $table->unsignedInteger('attempts')->default(0);
            $table->uuid('attempt_uuid')->nullable();
            $table->timestamp('lease_expires_at')->nullable();
            $table->timestamps();
            $table->unique(['runtime_context_id', 'idempotency_key']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('node_operations');
    }
};
