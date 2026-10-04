from django.db import models

# Minimal ProblemTag model for ManyToManyField
class ProblemTag(models.Model):
    tag_name = models.CharField(max_length=100, unique=True)
    def __str__(self):
        return self.tag_name
    
class Problem(models.Model):
    """
    Represents a programming problem migrated from Polygon.

    Fields:
        polygon_id (str): The unique Polygon problem ID.
        title (str): The problem title.
        slug (str): URL-friendly unique identifier for the problem.
        difficulty (str): The difficulty level (easy/medium/hard).
        avg_time_taken (float): Average time taken to solve the problem.
        total_submissions (int): Total number of submissions.
        problem_statement (str): The problem statement/legend.
        input_format (str): The input specification.
        output_format (str): The output specification.
        constraints (str): Constraints for the problem.
        editorial (str): Editorial content for the problem.
        time_limit (float): Time limit in milliseconds.
        problem_statement_url (str): URL to the problem statement.
        additional_info (str): Any additional information.
        extra_tags (ManyToMany[ProblemTag]): Tags associated with the problem.
        memory_limit (int): Memory limit in MB.
        checker_type (str): The checker type for the problem.
        test_case_count (int): Number of test cases.
        is_locked (bool): Whether the problem is locked.
        genie_assist (bool): Genie assist flag.
        genie_plus (bool): Genie plus flag.
        content_video_url (str): URL to content video.
        voice_assistant (str): Voice assistant field.
    """
    DIFFICULTY_CHOICES = [
        ('easy', 'Easy'),
        ('medium', 'Medium'),
        ('hard', 'Hard'),
    ]
    CHECKER_TYPE_CHOICES = [
        ('ncmp', 'ncmp'),
        ('fcmp', 'fcmp'),
        ('hcmp', 'hcmp'),
        ('lcmp', 'lcmp'),
        ('nyesno', 'nyesno'),
        ('rcmp4', 'rcmp4'),
        ('rcmp6', 'rcmp6'),
        ('rcmp9', 'rcmp9'),
        ('wcmp', 'wcmp'),
        ('yesno', 'yesno'),
        ('custom', 'custom'),
    ]
    polygon_id = models.CharField(max_length=100, unique=True, blank=True)
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=255)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES)
    avg_time_taken = models.FloatField(default=30.0)
    total_submissions = models.IntegerField(default=0)
    problem_statement = models.TextField(blank=True,null=True)
    input_format = models.TextField(blank=True,null=True)
    output_format = models.TextField(blank=True,null=True)
    constraints = models.TextField(blank=True, null=True)
    editorial = models.TextField(blank=True, null=True)
    time_limit = models.FloatField(default=1000,help_text="Time limit in milliseconds")
    problem_statement_url = models.URLField(blank=True, null=True)
    additional_info = models.TextField(blank=True, null=True)
    extra_tags = models.ManyToManyField(ProblemTag, blank=True, related_name='problems')
    memory_limit = models.IntegerField(default=256,help_text="Memory limit in MB")
    checker_type = models.CharField(max_length=10, choices=CHECKER_TYPE_CHOICES,default='ncmp')
    test_case_count = models.IntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    genie_assist = models.BooleanField(default=False)
    genie_plus = models.BooleanField(default=False)
    content_video_url = models.URLField(blank=True, null=True)
    voice_assistant = models.CharField(max_length=128, blank=True, null=True)
    notes = models.TextField(blank=True, null=True, help_text="Notes Section from Polygon")
    genie_chat = models.BooleanField(default=False)
    
    def __str__(self):
        """
        Returns the string representation of the Problem.

        Returns:
            str: The problem title.
        """
        return self.title
    
class SampleTestCase(models.Model):
    """
    Represents a sample test case for a problem, shown in the problem statement.

    Fields:
        problem (ForeignKey[Problem]): The related Problem.
        input (str): The input for the sample test case.
        output (str): The expected output for the sample test case.
        order (int): The order of the sample test case for display.
    """
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='sample_test_cases')
    input = models.TextField()
    output = models.TextField()
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ['problem', 'order']

    def __str__(self):
        """
        Returns the string representation of the SampleTestCase.

        Returns:
            str: Description of the sample test case and its problem.
        """
        return f"Sample Case {self.order} for {self.problem.title}"
    
class ProblemTestCase(models.Model):
    """
    Represents a test case for a problem, which may be a sample or a regular test case.

    Fields:
        problem (ForeignKey[Problem]): The related Problem.
        is_sample (bool): Whether this test case is a sample.
        input (str): The input for the test case.
        output (str): The expected output for the test case.
        description (str): Description of the test case.
        order (int): The order of the test case.
        created_at (datetime): When the test case was created.
        updated_at (datetime): When the test case was last updated.
    """
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, related_name='problem_test_cases')
    is_sample = models.BooleanField(default=False)
    input = models.TextField()
    output = models.TextField()
    description = models.TextField(blank=True, null=True)
    order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['problem', 'order']

    def __str__(self):
        """
        Returns the string representation of the ProblemTestCase.

        Returns:
            str: Description of the test case and its problem.
        """
        return f"Problem TestCase {self.order} for {self.problem.title}"