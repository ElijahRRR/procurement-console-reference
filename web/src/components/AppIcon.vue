<script setup lang="ts">
/**
 * EasyQuick 图标（Heroicons outline 风格，24×24、描边 1.5、currentColor）。
 * 尺寸：xs 12、sm 16、md 20（默认）、lg 24、xl 32。图标路径来自 design-system/icons.json。
 */
import { computed } from 'vue'
import icons from '@/design-system/icons.json'

export type IconName = keyof typeof icons
export type IconSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl'

const props = withDefaults(defineProps<{ name: IconName; size?: IconSize; label?: string }>(), { size: 'md', label: '' })

const PX: Record<IconSize, number> = { xs: 12, sm: 16, md: 20, lg: 24, xl: 32 }
const px = computed(() => PX[props.size])
const body = computed(() => (icons as Record<string, string>)[props.name] ?? '')
</script>

<template>
  <svg
    class="app-icon inline-block shrink-0"
    :width="px"
    :height="px"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    :role="label ? 'img' : undefined"
    :aria-label="label || undefined"
    :aria-hidden="label ? undefined : 'true'"
    v-html="body"
  />
</template>
