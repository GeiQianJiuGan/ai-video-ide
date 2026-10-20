<script setup lang="ts">
import WorkbenchLayout from './layout/WorkbenchLayout.vue'
import AccessGate from './AccessGate.vue'
</script>

<template>
  <AccessGate><WorkbenchLayout /></AccessGate>
</template>
