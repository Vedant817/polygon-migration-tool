from django.contrib import admin
from .models import Problem, SampleTestCase, ProblemTestCase, ProblemTag

class ProblemTagAdmin(admin.ModelAdmin):
    """
    Admin configuration for the ProblemTag model.
    """
    list_display = ('tag_name', 'problem_count')
    search_fields = ('tag_name',)
    ordering = ('tag_name',)
    
    def problem_count(self, obj):
        """Return the number of problems using this tag."""
        return obj.problems.count()
    problem_count.short_description = 'Number of Problems'

class ProblemAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Problem model.
    """
    list_display = ('id', 'polygon_id', 'title', 'difficulty', 'display_tags', 'test_case_count')
    list_filter = ('difficulty', 'extra_tags', 'is_locked', 'genie_assist', 'genie_plus')
    search_fields = ('title', 'polygon_id', 'slug')
    filter_horizontal = ('extra_tags',)  # This creates a nice widget for many-to-many
    readonly_fields = ('polygon_id', 'slug', 'avg_time_taken', 'total_submissions')
    fieldsets = (
        ('Basic Information', {
            'fields': ('polygon_id', 'title', 'slug', 'difficulty')
        }),
        ('Content', {
            'fields': ('problem_statement', 'input_format', 'output_format', 'constraints', 'editorial')
        }),
        ('Configuration', {
            'fields': ('time_limit', 'memory_limit', 'checker_type', 'test_case_count')
        }),
        ('Tags & Metadata', {
            'fields': ('extra_tags', 'is_locked', 'genie_assist', 'genie_plus')
        }),
        ('Additional Info', {
            'fields': ('problem_statement_url', 'content_video_url', 'voice_assistant', 'additional_info', 'notes'),
            'classes': ('collapse',)
        }),
        ('Statistics', {
            'fields': ('avg_time_taken', 'total_submissions'),
            'classes': ('collapse',)
        }),
    )
    
    def display_tags(self, obj):
        """Display tags as a comma-separated list."""
        return ", ".join([tag.tag_name for tag in obj.extra_tags.all()])
    display_tags.short_description = 'Tags'

class ProblemTestCaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'problem', 'input', 'output', 'description', 'is_sample')
    list_filter = ('problem', 'is_sample')
    search_fields = ('problem__title', 'input', 'output')
    list_editable = ('is_sample',)
    list_per_page = 100 

class SampleTestCaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'problem', 'input', 'output')
    list_filter = ('problem',)
    search_fields = ('problem__title', 'input', 'output')
    list_per_page = 100 

# Register your models here.
admin.site.register(Problem, ProblemAdmin)
admin.site.register(ProblemTag, ProblemTagAdmin)
admin.site.register(SampleTestCase, SampleTestCaseAdmin)
admin.site.register(ProblemTestCase, ProblemTestCaseAdmin)


