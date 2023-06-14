from odoo import models, fields, api
#from odoo.addons.queue_job.job import job
import odoorpc
import logging
import json
from odoo.exceptions import ValidationError
from odoo.tests import Form
import datetime
_log = logging.getLogger(__name__)
import requests
import base64

def get_chunks(iterable, n=1000):
    for i in range(0, len(iterable), int(n)):
        yield iterable[i:i + int(n)]

SESSION = requests.session()
# creating an user raises: 'You cannot create a new user from here.
#  To create new user please go to configuration panel.'

BASE_MODEL_PREFIX = ['ir.', 'mail.', 'base.', 'bus.', 'report.', 'account.', 'res.users', 'stock.location', 'res.',
                     'product.pricelist', 'product.product', 'stock.picking.type','uom.','crm.team', 'stock.warehouse', 'stock.picking']
# todo: add bool field on migration.model like use_same_id
#MODELS_WITH_EQUAL_IDS = ['res.partner', 'product.product', 'product.template', 'product.category', 'seller.instance', 'uom.uom', 'res.users']
MODELS_WITH_EQUAL_IDS = []
WITH_AUTO_PROCESS = ['sale.order', 'purchase.order', 'update_product_template_costs', 'account.invoice', 'stock.picking']
COMPUTED_FIELDS_TO_READ = ['invoice_ids']


class MigrationRecord(models.Model):
    _name = 'migration.record'
    _description = "Module for Record model of migrations"

    name = fields.Char()
    model = fields.Char(index=True)
    old_id = fields.Integer(index=True)
    new_id = fields.Integer(index=True)
    company_id = fields.Many2one('res.company', related='migration_model.company_id')
    data = fields.Text(help='Old data in JSON format')
    state = fields.Selection([('pending', 'Pending'),('done','Done'),('error', 'Error'),('by_system', 'Created by sistem')])
    migration_model =  fields.Many2one('migration.model')
    state_message = fields.Text()
    type = fields.Char()
    relation = fields.Char()

    def map_record(self):
        if self.new_id:
            return self.new_id
        model = self.migration_model.model or self.model
        company_id = self.company_id.id
        if not model:
            raise ValidationError('Model is required')
        data = False
        name_data = False
        if self.data:
            data = json.loads(self.data)
        new_id = self.get_new_id(model, self.old_id, company_id=company_id, create=False)
        if new_id:
            self.write({'new_id': new_id, 'model': model, 'state': 'done', })
            return new_id
        name_data = data.get(self.migration_model.alternative_name or 'name')
        name = self.name
        res_model = self.env[model]
        has_name = hasattr(res_model, 'name')
        alternative_name = self.migration_model.alternative_name or 'complete_name'
        has_complete_name = hasattr(res_model, alternative_name)
        if self.migration_model.match_records_by_name and (has_name or has_complete_name) and name:
            domain = [(alternative_name if has_complete_name else 'name', '=', name_data if name_data else name)]
            if self.migration_model.betwen_name_and_alternative:
                domain = ["|", ("name", "=", name), (alternative_name, '=', name_data)]
            has_company = hasattr(res_model, 'company_id')
            if self.migration_model.archived_record and hasattr(res_model, 'active'):
                domain.append(('active', 'in', [True, False]))
            if has_company and company_id:
                domain.append(('company_id', '=', company_id))
            new_rec = res_model.search(domain, limit=1).id

            if new_rec:
                self.write({'new_id': new_rec, 'model': model, 'state': 'done', })
                return new_rec

    def get_new_id(self, model, old_id, test=False, company_id=0, create=True):
        domain = [('model', '=', model), ('old_id', '=', old_id)]
        company_id = company_id or self.company_id.id
        if company_id:
            domain += ['|', ('company_id', '=', company_id), ('company_id', '=', False)]
        rec = self.search(domain)
        rec_new_id = rec.filtered(lambda r: r.new_id)
        if rec_new_id:
            return rec_new_id[0].new_id
        if len(rec) > 1:
            rec = rec[0]
        if rec.data and create:
            data = json.loads(rec.data)
            if company_id and data.get('company_id'):
                data['company_id'] = rec.company_id
            return rec.get_or_create_new_id(data, field_type=rec.type, relation=model, test=test, company_id=company_id)
        return 0

    def prepare_vals(self, data=None, fields_mapping=None, model='', test=False, company_id=0, migration_model=None):
        if not data and self.data:
            data = json.loads(self.data)
        elif data is None:
            data = {}
        if not fields_mapping and model:
            fields_mapping = self.env[model].fields_get()
        elif fields_mapping is None:
            fields_mapping = {}
        vals = {}
        migration_model = migration_model or self.migration_model
        in_status = migration_model.import_in_state
        company_id = company_id or self.company_id.id or migration_model.company_id.id
        omit_fields = migration_model.omit_fields.split(',') if migration_model.omit_fields else []

        for key in data:
            field_map = fields_mapping.get(key) or {}
            if not field_map or key in omit_fields:
                # Field does not exist in new odoo
                continue
            if key in ('id', 'display_name'):
                continue
            if key == 'company_id' and company_id:
                vals[key] = company_id
                continue
            if in_status and key == 'state':
                vals[key] = in_status
                continue
            if key == 'res_id' and model:
                vals[key] = self.env['migration.record'].search([('model','=', data.get('res_model')),('old_id','=',int(data[key]))], limit=1).new_id
                continue
            value = data[key]
            if isinstance(value, (list, tuple)):
                field_type = field_map.get('type')
                if field_type == 'many2one':
                    # value is a tuple with (id, name)
                    try:
                        new_id = self.browse().get_or_create_new_id(value, field_map=field_map, test=test, company_id=company_id)
                        if new_id:
                            vals[key] = new_id
                    except Exception as e:
                        _log.exception(e)
                        if test:
                            self.env.cr.rollback()
                        else:
                            self.env.cr.commit()  # to avoid InFailedSqlTransaction transaction block

                elif field_type == 'one2many':
                    # in this case we have to import first the relation
                    pass
                elif field_type == 'many2many':
                    related = field_map.get('relation')
                    if not related:
                        continue
                    values = [self.browse().get_or_create_new_id(
                        value=[old, ''], relation=related, field_type=field_type, flag_try_old_id=False,
                        test=test, company_id=migration_model.company_id.id) for old in value]
                    values = [v for v in values if v]
                    if values:
                        vals[key] = [[6, 0, values]]

            else:
                # simple value, int, str
                vals[key] = value

        if model == 'ir.attachment':
            try:
                access_token = data.get('access_token') or ''
                att_id = data.get('id')
                if att_id:
                    res = SESSION.get(f'https://{migration_model.credentials_id.url}/web/content/{att_id}?download=true&access_token={access_token}')
                    if res.status_code == 200:
                        vals['datas'] = base64.b64encode(res.content)
            except Exception as e:
                _log.error(e)
        return vals


    def get_or_create_new_id(self, value=None, field_map=False,  field_type='', relation='', flag_try_old_id=False, test=False, company_id=0, force_create=False):
        """
        :param flag_try_old_id:
        :param field_map: dict with keys: name, type, required, relation, etc
        :param value: tuple(id, name) or dict
        :raises: Exeption if fail creating record
        :return int
        """

        company_id = company_id or self.migration_model.company_id.id
        migration_model = False
        if self.new_id:
            return self.new_id
        if field_map:
            field_type = field_map.get('type')
            relation = field_map.get('relation')
        if not value:
            if self.data:
                value = json.loads(self.data)
            else:
                return 0
        if not relation:
            relation = self.model
        alternative_name = 'display_name' if relation == 'product.product' else 'complete_name'
        flag_try_old_id = flag_try_old_id or relation in MODELS_WITH_EQUAL_IDS
        res_model = self.env[relation]
        has_name = hasattr(res_model, 'name')
        has_complete_name = hasattr(res_model, alternative_name)
        new_rec = 0
        old_id = self.old_id
        raw_vals = False
        name = False
        if isinstance(value, (list,tuple)) and len(value) == 2:
            old_id = value[0]
            name = value[1]
            # if field map we can try to create the record here
            id = self.get_new_id(relation, old_id, company_id=company_id, create=bool(field_map))
            if id:
                return id
        elif isinstance(value, dict):
            old_id = value.get('id') or self.old_id
            name = value.get('name')
            raw_vals = value
            id = self.get_new_id(relation, old_id, create=bool(field_map))
            if id:
                return id
        if (not self.migration_model or self.migration_model.match_records_by_name) and (has_name or has_complete_name) and name:
            domain = [(alternative_name if has_complete_name else 'name', '=', name)]
            has_company = hasattr(res_model, 'company_id')
            rec_no_company = False  # some records are shared between companies
            if has_company and company_id:
                rec_no_company = res_model.search(domain + [('company_id', '=', False)], limit=1).id
                domain.append(('company_id', '=', company_id))
            new_rec = res_model.search(domain, limit=1).id
            if not new_rec and rec_no_company:
                new_rec = rec_no_company
        if not new_rec and flag_try_old_id:
            new_rec = res_model.browse(old_id).exists().id
        if not new_rec:
            omit = self.migration_model.only_fetch_data or ((relation and [True for r in BASE_MODEL_PREFIX if relation.startswith(r)]))
            allowed = force_create
            if omit and not allowed:
                _log.warning('try to create a forbidden model record %s for %s' % (relation, [old_id, name]))
                return 0
            if raw_vals:
                vals = self.prepare_vals(raw_vals, model=relation, company_id=company_id)
                try:
                    # if vals.get('notified_partner_ids'):
                    #     del vals['notified_partner_ids']
                    new_rec = res_model.with_context(no_vat_validation=True,force_create=True).create(vals).id
                except Exception as e:
                    if test:
                        self.env.cr.rollback()
                    else:
                        self.env.cr.commit()
                    _log.exception(e)
            elif old_id:
                # fetch data from old server
                try:
                    migration_domain = [('model', '=', relation)]
                    if company_id:
                        migration_domain += ['|', ('company_id', '=', company_id), ('company_id', '=', False)]
                    migration_model = self.migration_model.search(migration_domain, limit=1)
                    if migration_model:
                        if not migration_model.old_fields_list:
                            migration_model.compute_fields_mapping()
                        fields_to_read = json.loads(migration_model.old_fields_list)
                        fields_to_read.append('display_name')
                        old_model = migration_model.conn().env[relation]
                        data = old_model.search_read([('id', '=', old_id)], fields_to_read)
                        if data:
                            vals = self.prepare_vals(data[0], model=relation, company_id=company_id, migration_model=migration_model)
                            new_rec = res_model.fwith_context(force_create=True).create(vals).id
                    elif name:
                        new_rec = res_model.create({'name': name}).id
                except Exception as e:
                    if test:
                        self.env.cr.rollback()
                    else:
                        self.env.cr.commit()
                    _log.exception(e)
        if new_rec and old_id:
            try:
                if self.exists():
                    self.write({'new_id': new_rec, 'model': relation, 'state': 'done', 'type': field_type,
                                'relation': relation})
                else:
                    vals_to_create = {'new_id': new_rec, 'model': relation, 'old_id': old_id, 'state': 'done', 'type': field_type, 'relation': relation}
                    if migration_model:
                        vals_to_create['migration_model'] = migration_model.id
                    self.create([vals_to_create])
            except Exception as e:
                if test:
                    self.env.cr.rollback()
                else:
                    self.env.cr.commit()
                self.write({'state': 'error', 'state_message': repr(e)})
        return new_rec