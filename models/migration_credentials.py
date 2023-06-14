from odoo import models, fields

class MigrationCredentials(models.Model):
    _name = 'migration.credentials'
    _description = "Model for credentials migrations"

    database = fields.Char()
    url = fields.Char()
    port = fields.Char()
    user = fields.Char()
    password = fields.Char()
    protocol = fields.Selection([('jsonrpc', 'jsonrpc'),('jsonrpc+ssl', 'jsonrpc+ssl')], 'Protocol')