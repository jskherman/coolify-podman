<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('node_events', function (Blueprint $table) {
            $table->id();
            $table->string('uuid')->unique();
            $table->foreignId('runtime_context_id')->constrained()->restrictOnDelete();
            $table->unsignedBigInteger('sequence');
            $table->uuid('resource_id');
            $table->uuid('operation_uuid');
            $table->json('payload');
            $table->string('payload_hash', 64);
            $table->timestamps();
            $table->unique(['runtime_context_id', 'sequence']);
            $table->index(['runtime_context_id', 'resource_id', 'sequence']);
        });
        Schema::table('node_operations', function (Blueprint $table) {
            $table->unsignedBigInteger('last_node_sequence')->nullable();
            $table->unsignedBigInteger('cursor')->nullable();
        });
        Schema::table('runtime_contexts', function (Blueprint $table) {
            $table->json('event_cursors')->nullable();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('node_events');
        Schema::table('node_operations', function (Blueprint $table) {
            $table->dropColumn(['last_node_sequence', 'cursor']);
        });
        Schema::table('runtime_contexts', function (Blueprint $table) {
            $table->dropColumn('event_cursors');
        });
    }
};
