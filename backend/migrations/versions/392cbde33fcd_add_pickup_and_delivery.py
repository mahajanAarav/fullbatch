"""add pickup and delivery

Revision ID: 392cbde33fcd
Revises: ff187c2efbc2
Create Date: 2026-10-05 00:28:29.722014

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '392cbde33fcd'
down_revision: Union[str, Sequence[str], None] = 'ff187c2efbc2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing rows get sensible values: every old drop is pickup-only and every old order is a pickup order.
    op.add_column('drops', sa.Column('offers_pickup', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('drops', sa.Column('offers_delivery', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('drops', sa.Column('pickup_area', sa.String(length=120), nullable=True))
    op.add_column('drops', sa.Column('pickup_address', sa.String(length=300), nullable=True))
    op.add_column('drops', sa.Column('pickup_notes', sa.String(length=300), nullable=True))
    op.add_column('drops', sa.Column('pickup_lat', sa.Float(), nullable=True))
    op.add_column('drops', sa.Column('pickup_lng', sa.Float(), nullable=True))
    op.add_column('drops', sa.Column('delivery_radius_km', sa.Float(), nullable=True))
    op.add_column('drops', sa.Column('delivery_fee', sa.Numeric(precision=10, scale=2), nullable=False, server_default='0'))
    op.add_column('orders', sa.Column('fulfillment', sa.String(length=10), nullable=False, server_default='pickup'))
    op.add_column('orders', sa.Column('delivery_address', sa.String(length=300), nullable=True))
    op.add_column('orders', sa.Column('delivery_lat', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('delivery_lng', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('delivery_fee', sa.Numeric(precision=10, scale=2), nullable=False, server_default='0'))
    # Alembic does not detect check constraints, so they are added by hand.
    op.create_check_constraint('drop_offers_a_way_to_receive', 'drops', 'offers_pickup OR offers_delivery')
    op.create_check_constraint('drop_delivery_fee_not_negative', 'drops', 'delivery_fee >= 0')
    op.create_check_constraint('drop_delivery_radius_positive', 'drops', 'delivery_radius_km IS NULL OR delivery_radius_km > 0')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('drop_delivery_radius_positive', 'drops', type_='check')
    op.drop_constraint('drop_delivery_fee_not_negative', 'drops', type_='check')
    op.drop_constraint('drop_offers_a_way_to_receive', 'drops', type_='check')
    for column in ('delivery_fee', 'delivery_lng', 'delivery_lat', 'delivery_address', 'fulfillment'):
        op.drop_column('orders', column)
    for column in ('delivery_fee', 'delivery_radius_km', 'pickup_lng', 'pickup_lat', 'pickup_notes',
                   'pickup_address', 'pickup_area', 'offers_delivery', 'offers_pickup'):
        op.drop_column('drops', column)
