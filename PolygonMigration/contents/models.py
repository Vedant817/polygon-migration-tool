from django.db import models

# Create your models here.
class Topic(models.Model):
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=255)
    description = models.TextField(blank=True, null=True)
    overview_content = models.TextField(blank=True, null=True)
    crown_problem = models.ForeignKey('problems.Problem', on_delete=models.SET_NULL, null=True, blank=True, related_name='topic_crown')
    def __str__(self):
        return self.title