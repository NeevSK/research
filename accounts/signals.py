from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Organization, OrganizationMembership


@receiver(post_save, sender=Organization)
def ensure_organization_owner_membership(sender, instance, created, **kwargs):
    if created:
        OrganizationMembership.objects.get_or_create(
            user_id=instance.created_by_id,
            organization_id=instance.id,
            defaults={
                'role': OrganizationMembership.OWNER,
                'status': OrganizationMembership.ACTIVE,
            },
        )
